# NBRB bulletin scraper: new term deposits of households.
# Same structure as the Kufar scraper: small functions, Session, pandas.

from decimal import Decimal

import hashlib
import os
import re
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import pdfplumber
import argparse
import json
import shutil
from contextlib import nullcontext
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from pypdf import PdfReader

YEARS = list(range(2004, 2027))
DOWNLOAD = False  # Existing output/pdf cache; use --download for network downloads.
START_MONTH = 1
END_MONTH = 12
END_MONTH_BY_YEAR = {2026: 8}  # Set the latest published issue for incomplete years.
BASE_URL = "https://www.nbrb.by/publications/bulletin"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/pdf"}
MAX_PDF_BYTES = 50 * 1024 * 1024
MONTHS = 'январь февраль март апрель май июнь июль август сентябрь октябрь ноябрь декабрь'.split()
M = {month: i + 1 for i, month in enumerate(MONTHS)}
MP = '(?:' + '|'.join(MONTHS) + ')'
NUM = re.compile(r'(?<![\w,])(?:(?:[1-9]\d{0,2}(?:[ \u00a0]\d{3})+|\d+),\d+|\d+)')
FIELDS = ['month', 'new_term_byn_mln', 'new_term_fx_usd_mln', 'rate_byn_pct',
          'rate_fx_pct', 'usd_byn_avg', 'flow_share_fx', 'rate_spread_pp',
          'source', 'vintage', 'byn_page', 'fx_page', 'exchange_page', 'sha256',
          'publication_date', 'status', 'subtotal_check', 'fx_scope',
          'national_amount_raw_mln', 'national_currency_unit', 'national_to_byn_factor', 'unit_basis',
          'fx_amount_raw', 'fx_amount_unit', 'fx_to_usd_mln_factor', 'exchange_method', 'rate_status', 'notes']
VALUE_FIELDS = FIELDS[1:6]
FIELDS += [k + suffix for k in VALUE_FIELDS for suffix in ('_token', '_quantum')]
FIELDS += ['precision_status']


def _build_url(year: int, month: int) -> str:
    """Use the archive URL through December 2015, then the newer URL.

    Local cache filenames stay stat_bulletin_YYYY_MM.pdf for both formats.
    Downloading an old issue does not imply its tables can be extracted.
    """
    if type(year) is not int or not 2000 <= year <= datetime.now().year:
        raise ValueError("Expected an integer year from 2000 through the current year")
    if type(month) is not int or not 1 <= month <= 12:
        raise ValueError("Expected an integer month from 1 through 12")
    if year <= 2015:
        return f"{BASE_URL}/bulletin{year}_{month}.pdf"
    return f"{BASE_URL}/stat_bulletin_{year}_{month:02d}.pdf"


def _fetch(session, url: str, max_retries: int = 3,
           base_delay: float = 1.0) -> bytes | None:
    """Download a PDF; do not retry an absent issue (HTTP 404)."""
    for attempt in range(max_retries):
        try:
            with urlopen(Request(url, headers=HEADERS), timeout=30) as response:
                data = response.read(MAX_PDF_BYTES + 1)
            if len(data) > MAX_PDF_BYTES or not data.startswith(b"%PDF-"):
                raise ValueError("Not a PDF or PDF exceeds 50 MiB")
            return data
        except HTTPError as error:
            print(f"Attempt {attempt + 1}/{max_retries}: {url}: {error}")
            if error.code in (400, 401, 403, 404, 410):
                return None
        except (URLError, TimeoutError, OSError, ValueError) as error:
            print(f"Attempt {attempt + 1}/{max_retries}: {url}: {error}")
        if attempt + 1 < max_retries:
            time.sleep(base_delay * (attempt + 1))
    return None


def download_bulletin(session: object, year: int, month: int,
                      folder_path: str) -> Path | None:
    """Reuse a valid cache entry or download and atomically save a PDF."""
    folder = Path(folder_path)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"stat_bulletin_{year}_{month:02d}.pdf"
    if path.exists():
        with path.open("rb") as file:
            signature = file.read(5)
        if signature == b"%PDF-" and path.stat().st_size <= MAX_PDF_BYTES:
            print(f"Already cached: {path.name}")
            return path
        raise ValueError(f"Invalid cached PDF: {path}")
    data = _fetch(session, _build_url(year, month))
    if data is None and year >= 2016 and month < 10:
        data = _fetch(session, f"{BASE_URL}/stat_bulletin_{year}_{month}.pdf")
    if data is None:
        return None
    temporary = path.with_suffix(".pdf.part")
    temporary.write_bytes(data)
    temporary.replace(path)
    print(f"Downloaded: {path.name}")
    return path


def parse_numbers(s):
    return [float(v.replace(' ', '').replace('\xa0', '').replace(',', '.')) for v in NUM.findall(s)]


def number_tokens(text):
    """Retain trailing zeros: floats alone cannot encode publication precision."""
    return [v.replace(' ', '').replace('\xa0', '').replace(',', '.') for v in NUM.findall(text)]


def publication_quantum(token, factor=1):
    return str(Decimal(1).scaleb(Decimal(token).as_tuple().exponent) * Decimal(str(factor)))


def compare_published(rows, field):
    """Common closed rounding interval, in normalized units.

    Boundary overlap is only compatibility, never evidence of an actual rounding
    rule or of absence of revisions. Missing precision uses exact values only.
    The common intersection avoids accepting a chain of pairwise overlaps.
    """
    present = [r for r in rows if not pd.isna(r[field])]
    if len(present) != len(rows):
        return 'exact' if not present else 'conflict'
    values = [Decimal(str(r[field])) for r in present]
    if len(set(values)) <= 1:
        return 'exact'
    intervals = []
    for r, value in zip(present, values):
        q = r.get(field + '_quantum')
        q = Decimal(str(q)) if q is not None and not pd.isna(q) else Decimal(0)
        if q < 0 or not q.is_finite():
            raise ValueError('Invalid publication precision')
        intervals.append((value-q/2, value+q/2))
    lower = max(a for a,b in intervals)
    upper = min(b for a,b in intervals)
    return 'conflict' if lower > upper else 'rounding_boundary' if lower == upper else 'rounding_compatible'


def parse_columns(text, layout, vintage):
    years = sorted(set(map(int, re.findall('(20\\d\\d)\\s+год', text))))
    tokens_with_x = []
    for line in layout.splitlines():
        if line.lstrip().startswith('*'):
            continue
        for match in re.finditer(MP + '(?:\\s*[–—-]\\s*' + MP + ')?', line):
            tokens_with_x.append((match.start(), match.group()))
    tokens = [t for _, t in sorted(tokens_with_x)]
    if not 2 <= len(tokens) <= 6:
        raise ValueError('Ambiguous month header')
    mm = [None if re.search('[–—-]', t) else M[t] for t in tokens]
    if (len(years) == 1 and vintage.endswith('-01') and mm == [1,12,None,1]
            and years[0] == int(vintage[:4]) - 1):
        # Older January issues omit the current-year caption above the last column.
        yy = [years[0]] * 3 + [int(vintage[:4])]
    elif len(years) == 1:
        yy = [years[0]] * len(mm)
    elif len(years) == 2 and vintage.endswith('-01') and (mm == [1, 12, None, 1]) and (years == [int(vintage[:4]) - 1, int(vintage[:4])]):
        yy = [years[0]] * 3 + [years[1]]
    elif (len(years) == 2 and vintage.endswith('-02')
          and mm == [1, 2, 1, 2]
          and years == [int(vintage[:4]) - 1, int(vintage[:4])]):
        # February layout: Jan/Feb of the previous year, then Jan/Feb current.
        yy = [years[0], years[0], years[1], years[1]]
    else:
        raise ValueError('Unsupported multi-year layout')
    return [f'{y}-{m:02d}' if m else None for y, m in zip(yy, mm)]


def parse_pdf(path: Path) -> tuple[list[dict], list[dict]]:
    """Parse verified 2/3/4-sector layouts; preserve incomplete monthly rows."""
    path = Path(path)
    match = re.fullmatch(r'stat_bulletin_(20\d\d)_(\d{2})(?:\(\d+\))?\.pdf', path.name)
    if not match:
        raise ValueError('Expected normalized filename stat_bulletin_YYYY_MM.pdf')
    vintage = '-'.join(match.groups())
    texts = [page.extract_text() or '' for page in PdfReader(path).pages]
    table_kinds, table_scopes = {}, {}
    title_pattern = r'(?:новых банковских вкладов|вновь привлеч[её]нных (?:депозитов|вкладов\s*\(депозитов\)))'
    for text in texts:
        flat = ' '.join(text.split()).lower()
        # Exclude TOC and methodology pages that mention several currencies.
        if 'всего срочные' not in flat or not re.search(title_pattern, flat):
            continue
        title = re.search(r'таблица\s+(\d+\.\d+)', flat)
        if not title:
            continue
        if 'в национальной валюте' in flat:
            kind = 'byn'
        elif 'в иностранной валюте' in flat or 'в скв' in flat or 'свободно конвертируем' in flat:
            kind = 'fx'
        else:
            continue
        if title[1] in table_kinds and table_kinds[title[1]] != kind:
            raise ValueError('Conflicting actual table titles: ' + title[1])
        table_kinds[title[1]] = kind
        if kind == 'fx':
            table_scopes[title[1]] = ('freely_convertible_currency' if 'в скв' in flat or 'свободно конвертируем' in flat else 'all_foreign_currency')
    deposits = {'byn': {}, 'fx': {}}
    rates, annual, diagnostics = {}, [], []
    with pdfplumber.open(path) as pdf:
        # Extract both old average and newer calendar-day arithmetic tables.
        # Calendar arithmetic has priority, but the old method is labelled.
        previous_rate_table, rate_year = None, None
        for i, text in enumerate(texts):
            flat = ' '.join(text.split()).lower()
            tm = re.search(r'(?:таблица|таблицы)\s+(\d+\.\d+)', flat)
            rate_title = ('официальном курсе' in flat and ('среднем' in flat or 'средняя арифметическая' in flat)
                          and 'доллару сша' in flat and 'российскому рублю' in flat)
            continuation = bool(tm and previous_rate_table and tm[1] == previous_rate_table[0])
            if not rate_title and not continuation:
                continue
            if rate_title:
                # TOC/methodology prose is not an exchange-rate table.
                pt_rate = pdf.pages[i].extract_text() or ''
                if not re.search(r'^\s*' + MP + r'\s+\d', pt_rate, re.M):
                    continue
                if not all(c in flat for c in ['российскому рублю', 'доллару сша', 'евро']):
                    continue
                currency_order = sorted(['российскому рублю', 'доллару сша', 'евро'], key=flat.index)
                usd_index = currency_order.index('доллару сша')
                method = 'calendar_arithmetic' if 'средняя арифметическая' in flat else 'official_average_legacy'
                previous_rate_table = (tm[1], usd_index, method) if tm else None
                rate_year = None
            else:
                _, usd_index, method = previous_rate_table
            for line in (pdf.pages[i].extract_text() or '').splitlines():
                ym = re.match(r'^\s*(20\d\d)(?:\s+год)?\s*$', line)
                if ym:
                    rate_year = int(ym[1]); continue
                lm = re.match('^(' + MP + r')\s+(\d.*)$', line.strip())
                if lm and rate_year:
                    vals = parse_numbers(lm[2])
                    if len(vals) not in (3,4):
                        continue
                    key = f'{rate_year}-{M[lm[1]]:02d}'
                    # Rate unit follows the issue's denomination, not the month.
                    rate_factor = 0.0001 if vintage < '2016-07' else 1.0
                    token = number_tokens(lm[2])[usd_index]
                    value = (float(Decimal(token)*Decimal(str(rate_factor))), i+1, method,
                             token, publication_quantum(token, rate_factor))
                    if key not in rates or method == 'calendar_arithmetic':
                        rates[key] = value
        for i, text in enumerate(texts):
            flat = ' '.join(text.split()).lower()
            if 'всего срочные' not in flat:
                continue
            tm = re.search(r'(?:таблица|таблицы)\s+(\d+\.\d+)', flat)
            if not tm or tm[1] not in table_kinds:
                continue
            kind = table_kinds[tm[1]]
            pt = pdf.pages[i].extract_text() or ''
            header = pt[:pt.lower().find('до востребования')].lower() if 'до востребования' in pt.lower() else ' '.join(pt.splitlines()[:8]).lower()
            if 'физических лиц' not in flat:
                diagnostics.append(dict(page=i+1,error='Missing household label')); continue
            totals = [line for line in pt.splitlines() if re.match(r'^\s*всего срочные\b',line,re.I)]
            if len(totals) not in (2,3,4):
                diagnostics.append(dict(page=i+1,error='Unsupported sector row count')); continue
            # Verified orders: total/legal/households/IP (4), total/legal/households
            # or legal/SME/households (3), legal/households (2).
            household_index = 2 if len(totals) in (3,4) else 1
            short = [line for line in pt.splitlines() if re.match(r'^\s*(?:краткосрочные|на срок до 1 года)\b',line,re.I)]
            long = [line for line in pt.splitlines() if re.match(r'^\s*(?:долгосрочные|на срок свыше 1 года)\b',line,re.I)]
            if len(short)!=len(totals) or len(long)!=len(totals):
                diagnostics.append(dict(page=i+1,error='Missing short/long household rows')); continue
            scale = 1000.0 if 'млрд' in header else (0.001 if 'тыс.' in header else (1.0 if 'млн' in header else None))
            if scale is None:
                diagnostics.append(dict(page=i+1,error='Unknown amount unit')); continue
            if kind=='byn' and 'рублей' not in header or kind=='fx' and not ('долларов' in header and 'сша' in header):
                diagnostics.append(dict(page=i+1,error='Unknown currency unit')); continue
            factor = scale * (0.0001 if kind=='byn' and vintage<'2016-07' else 1.0)
            unit = ('billion' if scale==1000 else 'thousand' if scale==0.001 else 'million') + ('_BYR' if kind=='byn' and vintage<'2016-07' else '_BYN' if kind=='byn' else '_USD')
            try:
                periods = parse_columns(text, pdf.pages[i].extract_text(layout=True), vintage)
                tokens = number_tokens(totals[household_index])
                vals = parse_numbers(totals[household_index])
                sv = parse_numbers(re.sub(r'^\s*(?:краткосрочные|на срок до 1 года)\s*','',short[household_index],flags=re.I))
                lv = parse_numbers(re.sub(r'^\s*(?:долгосрочные|на срок свыше 1 года)\s*','',long[household_index],flags=re.I))
                if not (len(vals)==len(sv)==len(lv)==3*len(periods)):
                    raise ValueError('Subtotal/header numeric width mismatch')
                for j,period in enumerate(periods):
                    amount,share,rate = vals[3*j:3*j+3]
                    if amount<0 or abs(share-100)>0.01 or not 0<=rate<100:
                        raise ValueError('Invalid household total/share/rate')
                    if period is None:
                        annual.append(dict(source=path.name,page=i+1,kind=kind,amount=amount,rate=rate,label='cumulative_not_monthly',unit=unit));continue
                    consistent = abs(amount-sv[3*j]-lv[3*j]) <= 0.1500001
                    record = dict(amount=float(Decimal(tokens[3*j])*Decimal(str(factor))),raw=amount,rate=rate,page=i+1,consistent=consistent,
                                  amount_token=tokens[3*j], amount_quantum=publication_quantum(tokens[3*j],factor),
                                  rate_token=tokens[3*j+2], rate_quantum=publication_quantum(tokens[3*j+2]),
                                  unit=unit,factor=factor,scope=table_scopes.get(tm[1]),
                                  notes=' '.join(line for line in pt.splitlines() if line.lstrip().startswith('*')))
                    if period in deposits[kind] and deposits[kind][period]['amount']!=record['amount']:
                        diagnostics.append(dict(page=i+1,month=period,error='Conflicting amounts within source'));continue
                    deposits[kind][period] = record
            except ValueError as error:
                diagnostics.append(dict(page=i+1,error=str(error)))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    rows=[]
    for month in sorted(deposits['byn'].keys() & deposits['fx'].keys()):
        b,f = deposits['byn'][month], deposits['fx'][month]
        exchange, page, method, exchange_token, exchange_quantum = rates.get(
            month,(float('nan'),float('nan'),'missing','',None))
        denominator = b['amount']+f['amount']*exchange
        share = f['amount']*exchange/denominator if denominator>0 else float('nan')
        consistent = b['consistent'] and f['consistent']
        rows.append(dict(month=month,new_term_byn_mln=b['amount'],new_term_fx_usd_mln=f['amount'],
             rate_byn_pct=b['rate'],rate_fx_pct=f['rate'],usd_byn_avg=exchange,flow_share_fx=share,
             rate_spread_pp=b['rate']-f['rate'],source=path.name,vintage=vintage,byn_page=b['page'],fx_page=f['page'],
             exchange_page=page,sha256=digest,publication_date='',subtotal_check='passed' if consistent else 'failed',
             fx_scope=f['scope'],status='inconsistent_subtotal' if not consistent else 'missing_exchange_rate' if method=='missing' else 'parsed_needs_source_review',
             national_amount_raw_mln=b['raw'],national_currency_unit=b['unit'],national_to_byn_factor=b['factor'],
             unit_basis='explicit_header_scale_and_issue_denomination',fx_amount_raw=f['raw'],fx_amount_unit=f['unit'],
             fx_to_usd_mln_factor=f['factor'],exchange_method=method,rate_status='published',
             new_term_byn_mln_token=b['amount_token'],new_term_byn_mln_quantum=b['amount_quantum'],
             new_term_fx_usd_mln_token=f['amount_token'],new_term_fx_usd_mln_quantum=f['amount_quantum'],
             rate_byn_pct_token=b['rate_token'],rate_byn_pct_quantum=b['rate_quantum'],
             rate_fx_pct_token=f['rate_token'],rate_fx_pct_quantum=f['rate_quantum'],
             usd_byn_avg_token=exchange_token,usd_byn_avg_quantum=exchange_quantum,precision_status='source_tokens',notes=b['notes']+' '+f['notes']))
    for month in sorted(deposits['byn'].keys() ^ deposits['fx'].keys()):
        diagnostics.append(dict(month=month,error='Only one currency extracted'))
    for row in rows:
        if row['exchange_method']=='missing':
            diagnostics.append(dict(month=row['month'],error='Missing exchange rate; amounts preserved'))
    # Caller exports diagnostics; keep old two-value API.
    annual.extend(dict(source=path.name,label='parse_issue',**d) for d in diagnostics)
    if not rows:
        raise ValueError('No matched household months; '+ '; '.join(str(d) for d in diagnostics[:4]))
    return rows,annual


def cached_pdfs(folder: Path):
    """Recognize original archive filenames and normalized cache filenames."""
    found = []
    for path in sorted(folder.rglob("*.pdf")):
        name = path.name
        m = re.fullmatch(r"(?:stat_bulletin_(20\d{2})_|bulletin(20\d{2})_)(\d{1,2})(?:\(\d+\))?\.pdf", name, re.I)
        if m:
            year = int(m[1] or m[2]); month = int(m[3])
            if 1 <= month <= 12:
                found.append((path, year, month))
    return found


def choose_rows(rows):
    """Preserve matching volumes when only rates have been revised."""
    chosen, issues, groups = [], [], {}
    for row in rows:
        groups.setdefault((row['month'], row['fx_scope']), []).append(row)
    for (month,scope),versions in sorted(groups.items()):
        valid=[r for r in versions if r['subtotal_check']=='passed']
        for r in versions:
            if r['subtotal_check']!='passed':
                issues.append(dict(source=r['source'],month=month,error='Subtotal inconsistent; excluded'))
        if not valid:continue
        if month=='2024-09' and scope=='all_foreign_currency':
            correct=[r for r in valid if abs(r['new_term_byn_mln']-1001.4)<0.05 and abs(r['rate_byn_pct']-10.81)<0.005]
            if correct:valid=correct
        amount_comparisons = {k:compare_published(valid,k) for k in ['new_term_byn_mln','new_term_fx_usd_mln']}
        if 'conflict' in amount_comparisons.values():
            issues.append(dict(source=';'.join(r['source'] for r in valid),month=month,error='Conflicting published amounts; inspect sources.csv'));continue
        # Prefer a documented calendar arithmetic course; keep the method explicit.
        calendar=[r for r in valid if r.get('exchange_method')=='calendar_arithmetic' and not pd.isna(r['usd_byn_avg'])]
        available=[r for r in valid if not pd.isna(r['usd_byn_avg'])]
        candidates=calendar or available or valid
        exchange_comparison = compare_published(candidates,'usd_byn_avg')
        if exchange_comparison == 'conflict':
            issues.append(dict(source=';'.join(r['source'] for r in candidates),month=month,error='Conflicting exchange rates; inspect sources.csv'));continue
        row=max(candidates,key=lambda r:r['vintage']).copy()
        row['precision_status'] = json.dumps({**amount_comparisons, 'usd_byn_avg':exchange_comparison},ensure_ascii=False)
        disputed=[]
        rate_comparisons={}
        for k in ['rate_byn_pct','rate_fx_pct']:
            comparison = compare_published(valid,k)
            rate_comparisons[k] = comparison
            if comparison == 'conflict':
                row[k]=float('nan');row[k+'_token']='';row[k+'_quantum']=None;disputed.append(k)
        row['precision_status'] = json.dumps({**amount_comparisons,'usd_byn_avg':exchange_comparison,**rate_comparisons},ensure_ascii=False)
        if any(c.startswith('rounding_') for c in [*amount_comparisons.values(),exchange_comparison,*rate_comparisons.values()]):
            issues.append(dict(source=row['source'],month=month,error='Publication rounding compatible (not proof of no revision): '+row['precision_status']))
        if disputed:
            row['rate_spread_pp']=float('nan');row['rate_status']='conflicting_rates_volumes_preserved'
            issues.append(dict(source=row['source'],month=month,error='Revised rates: '+','.join(disputed)+'; matching amounts retained'))
        chosen.append(row)
    return chosen,issues


def fetch_and_save(folder_path: str, years=None, download=DOWNLOAD, pdf_folder=None):
    years = sorted(set(YEARS if years is None else years))
    if not years or any(type(y) is not int or y < 2004 or y > datetime.now().year for y in years):
        raise ValueError('Years must be integers from 2004 through the current year')
    output = Path(folder_path); output.mkdir(parents=True, exist_ok=True)
    previous = [output / name for name in ['deposits.csv','deposits_long.csv','sources.csv','issues.csv','coverage.csv','cumulative.csv','deposits_all_fx.csv','deposits_scv.csv'] if (output / name).exists()]
    if previous:
        backup = output / 'previous_results' / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        backup.mkdir(parents=True)
        for file in previous:
            shutil.copy2(file, backup / file.name)
    cache = Path(pdf_folder) if pdf_folder else output / 'pdf'
    cache.mkdir(parents=True, exist_ok=True)
    issues, rows, cumulative = [], [], []
    if download:
        for year in years:
            last = END_MONTH_BY_YEAR.get(year, 12)
            for month in range(1, last + 1):
                print(f'Processing bulletin {year}-{month:02d}', flush=True)
                try:
                    existing = [(p,y,m) for p,y,m in cached_pdfs(cache) if y==year and m==month]
                    if not existing and download_bulletin(None,year,month,str(cache)) is None:
                        issues.append(dict(source=_build_url(year,month),month='',error='Download failed'))
                except (OSError,ValueError) as error:
                    issues.append(dict(source=_build_url(year,month),month='',error=str(error)))
                time.sleep(0.5)
    entries = cached_pdfs(cache)
    # Normalize filenames in a separate cache; originals remain intact.
    normalized = output / '_normalized'; normalized.mkdir(exist_ok=True)
    seen = set()
    for path, year, month in entries:
        if year not in years and year - 1 not in years:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        normalized_path = normalized / f'stat_bulletin_{year}_{month:02d}.pdf'
        shutil.copyfile(path, normalized_path)
        print(f'Parsing {path.name}', flush=True)
        try:
            parsed, annual = parse_pdf(normalized_path)
            for row in parsed:
                row['source'] = str(path.resolve())
                if int(row['month'][:4]) in years:
                    rows.append(row)
            cumulative.extend(a for a in annual if a.get('label') != 'parse_issue')
            issues.extend(dict(source=str(path.resolve()),month=a.get('month',''),error=f"Page {a.get('page','')}: {a['error']}") for a in annual if a.get('label') == 'parse_issue')
        except Exception as error:
            issues.append(dict(source=str(path.resolve()),month='',error=str(error)))
            # Preserve unsupported source text for a new adapter rather than fabricate zeros.
            try:
                text = '\n\n'.join(f'PAGE {i+1}\n{p.extract_text() or ""}' for i,p in enumerate(PdfReader(path).pages))
                raw = output / 'unparsed_text';raw.mkdir(exist_ok=True)
                (raw / f'{year}_{month:02d}_{digest[:8]}.txt').write_text(text,encoding='utf-8')
            except Exception:
                pass
    selected, selection_issues = choose_rows(rows);issues.extend(selection_issues)
    monthly = pd.DataFrame(selected,columns=FIELDS)
    pd.DataFrame(rows,columns=FIELDS).to_csv(output/'sources.csv',index=False,encoding='utf-8-sig')
    monthly.to_csv(output/'deposits_long.csv',index=False,encoding='utf-8-sig')
    all_fx = monthly[monthly.fx_scope == 'all_foreign_currency'].drop(columns=['fx_scope'])
    scv = monthly[monthly.fx_scope == 'freely_convertible_currency'].drop(columns=['fx_scope'])
    scv = scv.rename(columns={k:k+'_scv' for k in scv.columns if k != 'month'})
    wide = all_fx.merge(scv,on='month',how='outer').sort_values('month')
    wide.to_csv(output/'deposits.csv',index=False,encoding='utf-8-sig')
    for scope, filename in [('all_foreign_currency','deposits_all_fx.csv'),('freely_convertible_currency','deposits_scv.csv')]:
        monthly[monthly.fx_scope == scope].to_csv(output/filename,index=False,encoding='utf-8-sig')
    pd.DataFrame(cumulative).to_csv(output/'cumulative.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(issues,columns=['source','month','error']).to_csv(output/'issues.csv',index=False,encoding='utf-8-sig')
    coverage = []
    for year in years:
        last = END_MONTH_BY_YEAR.get(year,12)
        for month in range(1,last+1):
            key=f'{year}-{month:02d}'
            current=monthly[monthly.month == key]
            coverage.append(dict(month=key,all_fx=bool((current.fx_scope=='all_foreign_currency').any()),
                                 scv=bool((current.fx_scope=='freely_convertible_currency').any())))
    pd.DataFrame(coverage).to_csv(output/'coverage.csv',index=False,encoding='utf-8-sig')
    print(f'Saved {len(monthly)} month/scope rows to {output.resolve()}',flush=True)
    print('See coverage.csv for missing months and issues.csv for failed sources.')
    return str(output/'deposits.csv')


if __name__ == '__main__':
    cli=argparse.ArgumentParser(description='NBRB household new term deposits')
    cli.add_argument('--years',nargs='+',type=int,default=YEARS)
    cli.add_argument('--pdf-dir',default=None,help='Folder containing existing PDFs; subfolders included')
    cli.add_argument('--output',default=str(Path(__file__).resolve().parent/'output'))
    cli.add_argument('--no-download',action='store_true')
    cli.add_argument('--download',action='store_true',help='Download missing PDF files')
    args=cli.parse_args()
    fetch_and_save(args.output,args.years,(args.download or DOWNLOAD) and not args.no_download,args.pdf_dir)
