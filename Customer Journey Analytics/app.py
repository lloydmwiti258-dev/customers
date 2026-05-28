import json
import os
import re
import threading
from datetime import datetime
from flask import Flask, jsonify, render_template_string, request
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build
import pandas as pd
from dateutil import parser as dateparser

app = Flask(__name__)
CORS(app)

# ── CONFIG ──────────────────────────────────────────────────────────────────
# On Vercel: set GOOGLE_CREDENTIALS env var to the full JSON key file contents.
# Locally: set JSON_FILE_PATH env var or fall back to the default path.
JSON_FILE_PATH = os.environ.get(
    'JSON_FILE_PATH',
    r'C:\Users\Administrator\Downloads\retention-485013-974e48474123.json'
)
SPREADSHEET_ID = os.environ.get(
    'SPREADSHEET_ID',
    '1zravAS7NoxjnV-2476eBhMitZYQmxWgef3JTbwD-Rag'
)
SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']


def _get_credentials():
    """Load Google credentials from env var (Vercel) or local JSON file."""
    raw = os.environ.get('GOOGLE_CREDENTIALS')
    if raw:
        info = json.loads(raw)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return service_account.Credentials.from_service_account_file(JSON_FILE_PATH, scopes=SCOPES)

REGION_MAP = {
    'Hazina': 'Nairobi CBD', 'Hilton': 'Nairobi CBD',
    'Starmall': 'Nairobi CBD', 'Ktda': 'Nairobi CBD',
    'Mombasa': 'Coastal Region',
    'Kakamega': 'Western & Nyanza', 'Kisumu': 'Western & Nyanza',
    'Kisii': 'Western & Nyanza', 'Busia': 'Western & Nyanza',
    'Meru': 'Central Region', 'Nanyuki': 'Central Region',
    'Thika': 'Central Region',
    'Eldoret': 'Rift Valley', 'Nakuru': 'Rift Valley',
    'Kitengela': 'Rift Valley', 'Rongai': 'Rift Valley',
    'Sinza': 'Diaspora', 'Tanzania': 'Diaspora', 'Uganda': 'Diaspora',
    'Website': 'Online', 'Rejects': 'Reject'
}

# Meta: facebook, instagram, ig, liz, and anything containing "meta"
META_KEYWORDS = {'facebook', 'instagram', 'meta', 'liz'}
META_EXACT    = {'ig'}

# TikTok: tiktok ad, tiktok direct, tik tok …
TIKTOK_SOURCES = {'tik tok', 'tiktok'}

# Twitter
TWITTER_KEYWORDS = {'twitter'}

# Organic: anything containing "direct" (covers direct, instagram direct,
#          e-direct, new direct …) plus existing customers
ORGANIC_EXACT = {'existing'}


def normalize_source(src):
    if not src:
        return ''
    return str(src).strip().lower()


def classify_source(src):
    s = normalize_source(src)
    if not s:
        return 'other'
    # TikTok paid
    if any(k in s for k in TIKTOK_SOURCES):
        return 'tiktok'
    # Organic — "direct" in any form, plus existing customers
    if 'direct' in s or s in ORGANIC_EXACT:
        return 'organic'
    # Meta paid social — keyword match or exact 'ig'
    if s in META_EXACT or any(k in s for k in META_KEYWORDS):
        return 'meta'
    # Twitter
    if any(k in s for k in TWITTER_KEYWORDS):
        return 'twitter'
    # Everything else
    return 'other'


def normalize_phone(p):
    if not p:
        return ''
    p = re.sub(r'[\s\-\(\)\+]', '', str(p))
    if p.startswith('254') and len(p) >= 12:
        return '0' + p[3:]
    if p.startswith('7') and len(p) == 9:
        return '0' + p
    return p


# Pre-compiled regex patterns cover ~99 % of Google Sheets date formats.
# Matching these is ~1000× faster than dateutil.parser.parse.
_RE_YMD  = re.compile(r'^(\d{4})[/\-.](0?[1-9]|1[0-2])[/\-.](0?[1-9]|[12]\d|3[01])')
_RE_DMY  = re.compile(r'^(0?[1-9]|[12]\d|3[01])[/\-.](0?[1-9]|1[0-2])[/\-.](\d{4})')
_RE_DMY2 = re.compile(r'^(0?[1-9]|[12]\d|3[01])[/\-.](0?[1-9]|1[0-2])[/\-.](\d{2})$')


def safe_date(val):
    if not val:
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        m = _RE_YMD.match(s)
        if m:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = _RE_DMY.match(s)
        if m:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        m = _RE_DMY2.match(s)
        if m:
            return datetime(2000 + int(m.group(3)), int(m.group(2)), int(m.group(1)))
        return dateparser.parse(s, dayfirst=True)   # rare fallback
    except Exception:
        return None


def _process_df(df, phone_col, date_col, source_col):
    """Add date_parsed / phone_norm / source_class columns using unique-value maps.
    Calling normalize_phone / classify_source once per *unique* value instead of
    once per row can cut ~100 000 calls down to a few hundred."""
    df = df.copy()
    # Dates — map unique raw strings to datetime objects
    uq_dates = df[date_col].unique()
    df['date_parsed'] = df[date_col].map({d: safe_date(d) for d in uq_dates})
    # Phones — map unique raw strings to normalised form
    uq_phones = df[phone_col].unique()
    df['phone_norm'] = df[phone_col].map(
        {p: normalize_phone(p) for p in uq_phones}
    ).fillna('')
    # Sources — only a handful of unique values
    uq_srcs = df[source_col].unique()
    df['source_class'] = df[source_col].map(
        {s: classify_source(s) for s in uq_srcs}
    ).fillna('other')
    return df


def load_data():
    creds = _get_credentials()
    service = build('sheets', 'v4', credentials=creds)

    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    sheet_names = [s['properties']['title'] for s in meta['sheets']]

    SKIP = {'leads_2025', 'whatsapp', 'sheet2', 'sheet3'}
    shop_names = [n for n in sheet_names if n.strip().lower() not in SKIP]
    leads_sheet = next((n for n in sheet_names if 'lead' in n.lower()), None)
    wa_sheet = next((n for n in sheet_names
                     if 'whatsapp' in n.lower() or 'whats' in n.lower()), None)

    # Fetch all sheets in ONE batchGet call instead of N separate calls
    ranges = [f"'{n}'!A:K" for n in shop_names]
    leads_idx = wa_idx = None
    if leads_sheet:
        leads_idx = len(ranges)
        ranges.append(f"'{leads_sheet}'!A:F")
    if wa_sheet:
        wa_idx = len(ranges)
        ranges.append(f"'{wa_sheet}'!A:F")

    if not ranges:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    batch = service.spreadsheets().values().batchGet(
        spreadsheetId=SPREADSHEET_ID, ranges=ranges
    ).execute()
    vranges = batch.get('valueRanges', [])

    def parse_vrange(vr, col_names=None):
        rows = vr.get('values', [])
        if not rows:
            return pd.DataFrame()
        headers = rows[0]
        data = rows[1:]
        padded = [r + [''] * (len(headers) - len(r)) for r in data]
        df = pd.DataFrame(padded, columns=headers)
        if col_names:
            df.columns = list(col_names)[:len(df.columns)]
        return df

    SHOP_COLS = {0: 'date', 1: 'first_name', 2: 'gender', 3: 'phone',
                 4: 'product', 5: 'color', 6: 'category', 7: 'location',
                 8: 'price', 9: 'meta_spend', 10: 'tiktok_spend'}

    shop_frames = []
    for i, name in enumerate(shop_names):
        if i >= len(vranges):
            break
        df = parse_vrange(vranges[i])
        if df.empty:
            continue
        df.columns = [SHOP_COLS.get(j, df.columns[j]) for j in range(len(df.columns))]
        df['shop'] = name
        df['region'] = REGION_MAP.get(name, 'Other')
        shop_frames.append(df)

    shops_df = pd.concat(shop_frames, ignore_index=True) if shop_frames else pd.DataFrame()

    leads_df = pd.DataFrame()
    if leads_idx is not None and leads_idx < len(vranges):
        leads_df = parse_vrange(vranges[leads_idx],
                                ['date', 'contact', 'name', 'branch', 'source', 'platform'])

    wa_df = pd.DataFrame()
    if wa_idx is not None and wa_idx < len(vranges):
        wa_df = parse_vrange(vranges[wa_idx],
                             ['date', 'name', 'contact', 'source', 'activity', 'branch'])

    return shops_df, leads_df, wa_df


def compute_analytics(shops_df, leads_df, wa_df):
    now = datetime.now()
    results = {}

    # ── Parse / normalise (skipped when _refresh_cache already processed frames)
    if not leads_df.empty and 'date_parsed' not in leads_df.columns:
        leads_df = _process_df(leads_df, 'contact', 'date', 'source')
    elif not leads_df.empty:
        leads_df = leads_df.copy()

    if not wa_df.empty and 'date_parsed' not in wa_df.columns:
        wa_df = _process_df(wa_df, 'contact', 'date', 'source')
    elif not wa_df.empty:
        wa_df = wa_df.copy()

    if not shops_df.empty and 'date_parsed' not in shops_df.columns:
        shops_df = shops_df.copy()
        shops_df['date_parsed'] = shops_df['date'].map(
            {d: safe_date(d) for d in shops_df['date'].unique()})
        shops_df['phone_norm'] = shops_df['phone'].map(
            {p: normalize_phone(p) for p in shops_df['phone'].unique()}).fillna('')
        try:
            shops_df['price_num'] = pd.to_numeric(
                shops_df['price'].str.replace(',', ''), errors='coerce').fillna(0)
            shops_df['meta_spend_num'] = pd.to_numeric(
                shops_df['meta_spend'].str.replace(',', ''), errors='coerce').fillna(0)
            shops_df['tiktok_spend_num'] = pd.to_numeric(
                shops_df['tiktok_spend'].str.replace(',', ''), errors='coerce').fillna(0)
        except Exception:
            shops_df['price_num'] = 0
            shops_df['meta_spend_num'] = 0
            shops_df['tiktok_spend_num'] = 0
    elif not shops_df.empty:
        shops_df = shops_df.copy()

    # ── Key phone sets (computed once, reused throughout) ────────────────────
    converted_phones = set()
    if not shops_df.empty and 'phone_norm' in shops_df.columns:
        converted_phones = set(shops_df['phone_norm'].dropna().unique()) - {''}

    all_lead_phones = set()
    if not leads_df.empty and 'phone_norm' in leads_df.columns:
        all_lead_phones.update(leads_df['phone_norm'].dropna())
    if not wa_df.empty and 'phone_norm' in wa_df.columns:
        all_lead_phones.update(wa_df['phone_norm'].dropna())
    all_lead_phones.discard('')

    # ── Pre-compute shared lookups (reused by sections 2, 3, 4, 8) ───────────
    # Single sort+dedup over the combined leads+WA frame instead of 3 separate ones
    _ft_parts = []
    for _df in (leads_df, wa_df):
        if _df.empty or 'phone_norm' not in _df.columns:
            continue
        _cols = [c for c in ['phone_norm', 'date_parsed', 'name', 'source', 'source_class']
                 if c in _df.columns]
        _ft_parts.append(_df[_cols].copy())
    first_touch_df = pd.DataFrame()
    if _ft_parts:
        _ft = pd.concat(_ft_parts, ignore_index=True)
        _ft = _ft[_ft['phone_norm'].fillna('') != '']
        _ft_dated = (_ft[_ft['date_parsed'].notna()]
                     .sort_values('date_parsed')
                     .drop_duplicates('phone_norm'))
        _ft_undated = _ft[_ft['date_parsed'].isna()].drop_duplicates('phone_norm')
        _ft_undated = _ft_undated[~_ft_undated['phone_norm'].isin(_ft_dated['phone_norm'])]
        first_touch_df = pd.concat([_ft_dated, _ft_undated], ignore_index=True)

    # Revenue pre-aggregated per phone — avoids N full-scan isin() calls in sections 3 & 8
    revenue_by_phone = {}
    if not shops_df.empty and 'price_num' in shops_df.columns and 'phone_norm' in shops_df.columns:
        revenue_by_phone = (shops_df[shops_df['phone_norm'] != '']
                            .groupby('phone_norm')['price_num'].sum()
                            .to_dict())

    # ── 1. Total Leads ────────────────────────────────────────────────────────
    wa_leads = len(wa_df) if not wa_df.empty else 0
    results['total_leads'] = len(all_lead_phones)
    results['total_wa_engagements'] = wa_leads

    # ── 2. Source Lead Generation ─────────────────────────────────────────────
    # Build a combined first-touch frame: leads_df + wa_df, deduped by phone
    # so each unique contact is counted once under their earliest known source.
    src_parts = []
    for df in (leads_df, wa_df):
        if df.empty or 'source' not in df.columns:
            continue
        cols = ['phone_norm', 'date_parsed', 'source', 'source_class']
        src_parts.append(df[[c for c in cols if c in df.columns]].copy())

    if src_parts:
        src_combined = pd.concat(src_parts, ignore_index=True)
        src_combined = src_combined[src_combined['phone_norm'].fillna('') != '']
        # Keep earliest-dated row per phone; rows without a date fall back last
        with_date = (src_combined[src_combined['date_parsed'].notna()]
                     .sort_values('date_parsed')
                     .drop_duplicates('phone_norm'))
        no_date = src_combined[src_combined['date_parsed'].isna()].drop_duplicates('phone_norm')
        no_date = no_date[~no_date['phone_norm'].isin(with_date['phone_norm'])]
        src_deduped = pd.concat([with_date, no_date], ignore_index=True)

        src_counts   = src_deduped['source'].str.strip().str.lower().value_counts().to_dict()
        class_counts = src_deduped['source_class'].value_counts().to_dict()
        results['source_breakdown']       = src_counts
        results['source_class_breakdown'] = class_counts
    else:
        results['source_breakdown']       = {}
        results['source_class_breakdown'] = {}

    # ── 3. Branch Performance ─────────────────────────────────────────────────
    branch_leads = {}
    if not leads_df.empty and 'branch' in leads_df.columns:
        branch_leads = leads_df['branch'].str.strip().value_counts().to_dict()

    # WA follow-up activity per branch
    branch_wa = {}
    branch_activities = {}
    if not wa_df.empty and 'branch' in wa_df.columns:
        wa_b = wa_df.copy()
        wa_b['branch'] = wa_b['branch'].str.strip()
        branch_wa = wa_b['branch'].value_counts().to_dict()
        if 'activity' in wa_b.columns:
            for branch, grp in wa_b.groupby('branch'):
                branch_activities[branch] = (
                    grp['activity'].str.strip().value_counts().head(5).to_dict()
                )

    # Per-branch unique lead phones (leads sheet + whatsapp sheet)
    branch_lead_phones = {}
    if not leads_df.empty and 'branch' in leads_df.columns:
        valid = leads_df[leads_df['phone_norm'] != '']
        for b, grp in valid.groupby(valid['branch'].str.strip()):
            branch_lead_phones.setdefault(b, set()).update(grp['phone_norm'])
    if not wa_df.empty and 'branch' in wa_df.columns:
        wa_b2 = wa_df[wa_df['phone_norm'] != ''].copy()
        wa_b2['branch'] = wa_b2['branch'].str.strip()
        for b, grp in wa_b2.groupby('branch'):
            branch_lead_phones.setdefault(b, set()).update(grp['phone_norm'])

    region_conv = {}
    if not shops_df.empty and 'shop' in shops_df.columns:
        region_conv = shops_df['region'].value_counts().to_dict()

    all_branches = set(
        list(branch_leads.keys()) + list(branch_lead_phones.keys()) + list(branch_wa.keys())
    )
    branch_perf = []
    for b in sorted(all_branches):
        leads_n = branch_leads.get(b, 0)
        wa_n = branch_wa.get(b, 0)
        b_phones = branch_lead_phones.get(b, set())
        # Conversions = leads shared to this branch whose phone appears in shops sheet
        converted_b = b_phones & converted_phones
        conv_n = len(converted_b)
        # Revenue = only from those lead-sourced sales (not all shop revenue)
        revenue = 0.0
        if converted_b and not shops_df.empty and 'price_num' in shops_df.columns:
            revenue = float(
                shops_df[shops_df['phone_norm'].isin(converted_b)]['price_num'].sum()
            )
        # total_contacts = unique phones from leads_2025 + whatsapp for this branch
        # conversions is always a subset of b_phones, so conv_n <= total_contacts guaranteed
        total_contacts = len(b_phones)
        rate = round(conv_n / total_contacts * 100, 1) if total_contacts > 0 else 0
        branch_perf.append({
            'branch': b,
            'region': REGION_MAP.get(b, 'Other'),
            'leads': total_contacts,       # unique leads for this branch (denominator)
            'leads_2025_rows': leads_n,    # raw row count from leads_2025 sheet (for reference)
            'wa_engagements': wa_n,
            'activities': branch_activities.get(b, {}),
            'conversions': conv_n,         # always <= leads (subset of b_phones)
            'not_converted': total_contacts - conv_n,
            'revenue': revenue,
            'rate': rate,
        })
    results['branch_performance'] = branch_perf
    results['region_conversions'] = region_conv

    # ── 4. Customer Journey (leads + WA → shops, O(n) phone-index) ───────────
    journey_times = []
    matched_journeys = []

    # Combine leads_2025 + whatsapp into one contacts frame; keep earliest
    # contact date per phone so delta = true time from first touch to purchase
    contact_parts = []
    if not leads_df.empty and 'phone_norm' in leads_df.columns:
        contact_parts.append(
            leads_df[['phone_norm', 'date_parsed', 'name', 'source']].copy()
        )
    if not wa_df.empty and 'phone_norm' in wa_df.columns:
        contact_parts.append(
            wa_df[['phone_norm', 'date_parsed', 'name', 'source']].copy()
        )

    if contact_parts and not shops_df.empty:
        all_contacts = pd.concat(contact_parts, ignore_index=True)
        all_contacts = all_contacts[
            all_contacts['date_parsed'].notna() & (all_contacts['phone_norm'] != '')
        ].sort_values('date_parsed').drop_duplicates('phone_norm')

        # Vectorised merge replaces Python iterrows — ~50× faster on large frames
        shops_j = shops_df[shops_df['phone_norm'] != '']\
            [['phone_norm', 'date_parsed', 'location', 'shop']]\
            .rename(columns={'date_parsed': 'conv_date',
                             'location': 'conv_loc', 'shop': 'conv_shop'})
        merged = all_contacts.merge(shops_j, on='phone_norm')
        merged = merged[merged['conv_date'] >= merged['date_parsed']].copy()
        if not merged.empty:
            merged['delta'] = (merged['conv_date'] - merged['date_parsed']).dt.days
            journey_times   = merged['delta'].tolist()
            merged['lead_date_s'] = merged['date_parsed'].dt.date.astype(str)
            merged['conv_date_s'] = merged['conv_date'].dt.date.astype(str)
            merged['shop_name']   = merged['conv_loc'].fillna('').str.strip()\
                .where(merged['conv_loc'].fillna('') != '', merged['conv_shop'].fillna(''))
            matched_journeys = merged[
                ['name', 'phone_norm', 'lead_date_s', 'conv_date_s', 'delta', 'shop_name', 'source']
            ].rename(columns={
                'phone_norm':   'phone',
                'lead_date_s':  'lead_date',
                'conv_date_s':  'conv_date',
                'delta':        'days_to_convert',
                'shop_name':    'shop',
            }).to_dict('records')

    if journey_times:
        jt = pd.Series(journey_times)
        results['avg_journey_days'] = round(float(jt.mean()), 1)
        results['min_journey_days'] = int(jt.min())
        results['max_journey_days'] = int(jt.max())
        results['journey_distribution'] = {
            'same_day':   int((jt == 0).sum()),
            '1_7_days':   int(((jt >= 1)  & (jt <= 7)).sum()),
            '8_30_days':  int(((jt >= 8)  & (jt <= 30)).sum()),
            '31_90_days': int(((jt >= 31) & (jt <= 90)).sum()),
            '90_plus':    int((jt > 90).sum()),
        }
    else:
        results['avg_journey_days'] = None
        results['journey_distribution'] = {}
    results['matched_journeys'] = matched_journeys[:500]

    # ── 4b. Conversion speed per source ──────────────────────────────────────
    # Group journey times by raw source and by source class
    raw_speed_map = {}
    cls_speed_map = {}
    for j in matched_journeys:
        src = j.get('source', '').strip().lower() or 'unknown'
        raw_speed_map.setdefault(src, []).append(j['days_to_convert'])
        cls = classify_source(j.get('source', ''))
        cls_speed_map.setdefault(cls, []).append(j['days_to_convert'])

    def speed_stats(times_map):
        rows = []
        for src, times in times_map.items():
            rows.append({
                'source': src,
                'count': len(times),
                'avg_days': round(sum(times) / len(times), 1),
                'min_days': min(times),
                'max_days': max(times),
            })
        return sorted(rows, key=lambda x: x['avg_days'])

    results['source_conversion_speed'] = speed_stats(raw_speed_map)
    results['class_conversion_speed']  = speed_stats(cls_speed_map)

    # ── 5. Unique Leads ───────────────────────────────────────────────────────
    results['unique_leads'] = len(all_lead_phones)

    # ── 6. Lead Matching & Conversion Rate ───────────────────────────────────
    # Unique leads (leads_2025 ∪ whatsapp) whose phone appears in shops sheet
    matched_converted = all_lead_phones & converted_phones
    results['leads_converted'] = len(matched_converted)   # leads who bought (phone-matched)
    results['total_shop_sales'] = len(shops_df) if not shops_df.empty else 0  # all sales incl. walk-ins
    results['conversion_rate'] = round(
        len(matched_converted) / len(all_lead_phones) * 100, 2
    ) if all_lead_phones else 0

    # Detail rows: every shop-sheet sale that belongs to a tracked lead
    converted_lead_details = []
    if not shops_df.empty and matched_converted:
        # Phone → lead info lookup for name/source enrichment
        phone_to_lead = {}
        if not leads_df.empty and 'phone_norm' in leads_df.columns:
            for row in leads_df[leads_df['phone_norm'].isin(matched_converted)].to_dict('records'):
                phone_to_lead.setdefault(row['phone_norm'], row)
        if not wa_df.empty and 'phone_norm' in wa_df.columns:
            for row in wa_df[wa_df['phone_norm'].isin(matched_converted)].to_dict('records'):
                phone_to_lead.setdefault(row['phone_norm'], row)

        for row in shops_df[shops_df['phone_norm'].isin(matched_converted)].to_dict('records'):
            phone = row['phone_norm']
            lead_info = phone_to_lead.get(phone, {})
            d = row.get('date_parsed')
            converted_lead_details.append({
                'name': lead_info.get('name', row.get('first_name', '')),
                'phone': phone,
                'source': lead_info.get('source', ''),
                'shop': row.get('location', '') or row.get('shop', ''),
                'product': row.get('product', ''),
                'date': str(d.date()) if d and pd.notnull(d) else '',
                'revenue': float(row.get('price_num', 0)),
            })
    results['converted_lead_details'] = converted_lead_details[:200]

    # ── 6b. Not Converted Analysis ────────────────────────────────────────────
    not_converted_phones = all_lead_phones - matched_converted

    nc_parts = []
    if not leads_df.empty and 'phone_norm' in leads_df.columns:
        cols = ['phone_norm', 'date_parsed', 'name', 'source', 'source_class']
        if 'branch' in leads_df.columns:
            cols.append('branch')
        sub = leads_df[cols].copy()
        if 'branch' not in sub.columns:
            sub['branch'] = ''
        nc_parts.append(sub)
    if not wa_df.empty and 'phone_norm' in wa_df.columns:
        cols = ['phone_norm', 'date_parsed', 'name', 'source', 'source_class']
        if 'branch' in wa_df.columns:
            cols.append('branch')
        sub = wa_df[cols].copy()
        if 'branch' not in sub.columns:
            sub['branch'] = ''
        nc_parts.append(sub)

    not_conv_details = []
    not_conv_by_class = {}
    not_conv_by_source = {}
    not_conv_by_branch = {}

    if nc_parts:
        nc_df = pd.concat(nc_parts, ignore_index=True)
        nc_df = nc_df[
            nc_df['phone_norm'].isin(not_converted_phones) &
            (nc_df['phone_norm'] != '') &
            nc_df['date_parsed'].notna()
        ].sort_values('date_parsed', ascending=False).drop_duplicates('phone_norm')

        # Vectorised days_since — dt.days is ~100× faster than .apply(lambda)
        nc_df['days_since'] = (pd.Timestamp(now) - nc_df['date_parsed']).dt.days
        nc_df = nc_df[nc_df['days_since'] >= 0]   # drop future-dated entries
        not_conv_by_class = nc_df['source_class'].fillna('other').value_counts().to_dict()
        not_conv_by_source = (
            nc_df['source'].fillna('').str.strip().str.lower()
            .replace('', 'unknown').value_counts().head(20).to_dict()
        )
        not_conv_by_branch = (
            nc_df['branch'].fillna('').str.strip()
            .replace('', 'Unknown').value_counts().head(20).to_dict()
        )
        # Vectorised period counts using pd.cut — replaces Python for-loop
        bins   = [-1, 30, 90, 180, 365, float('inf')]
        labels = ['d0_30', 'd31_90', 'd91_180', 'd181_365', 'over_365']
        cut    = pd.cut(nc_df['days_since'], bins=bins, labels=labels)
        vc     = cut.value_counts()
        pc = {k: int(vc.get(k, 0)) for k in labels}

        # Only send the 2 000 most-recently-contacted rows to the browser.
        # Period KPI cards use the pre-computed counts above so all 76k are accounted for.
        nc_sorted = nc_df.sort_values('days_since', ascending=True)
        for r in nc_sorted.head(2000).to_dict('records'):
            dp = r.get('date_parsed')
            not_conv_details.append({
                'name': str(r.get('name', '') or '').strip(),
                'phone': r.get('phone_norm', ''),
                'source': str(r.get('source', '') or '').strip(),
                'source_class': str(r.get('source_class', '') or '').strip(),
                'branch': str(r.get('branch', '') or '').strip(),
                'days_since': int(r.get('days_since', 0)),
                'last_contact': str(dp.date()) if dp else '',
            })

    results['not_converted_count']        = len(not_converted_phones)
    results['not_converted_periods']      = pc if nc_parts else {}
    results['not_converted_details_total']= len(not_conv_details)  # rows actually sent
    results['not_converted_by_class']     = not_conv_by_class
    results['not_converted_by_source']    = not_conv_by_source
    results['not_converted_by_branch']    = not_conv_by_branch
    results['not_converted_details']      = not_conv_details

    # ── 7. Lead Status (combined leads + whatsapp, most-recent contact per phone)
    hot, warm, cold = [], [], []
    contact_frames = []
    if not leads_df.empty and 'phone_norm' in leads_df.columns:
        f = leads_df[['phone_norm', 'date_parsed', 'name', 'source', 'branch']].copy()
        contact_frames.append(f)
    if not wa_df.empty and 'phone_norm' in wa_df.columns:
        f = wa_df[['phone_norm', 'date_parsed', 'name', 'source']].copy()
        f['branch'] = wa_df['branch'] if 'branch' in wa_df.columns else ''
        contact_frames.append(f)

    if contact_frames:
        combined = pd.concat(contact_frames, ignore_index=True)
        # Keep the most-recent touchpoint per phone to drive the status
        combined = combined[combined['date_parsed'].notna()].sort_values(
            'date_parsed', ascending=False).drop_duplicates('phone_norm')
        mask = (
            (~combined['phone_norm'].isin(converted_phones)) &
            (combined['phone_norm'] != '')
        )
        active = combined[mask].copy()
        active['days_since'] = active['date_parsed'].apply(lambda d: (now - d).days)

        def to_lead_list(df):
            return [
                {'name': r.get('name', ''), 'phone': r.get('phone_norm', ''),
                 'source': r.get('source', ''), 'days': int(r['days_since']),
                 'branch': r.get('branch', '')}
                for r in df.to_dict('records')
            ]

        hot = to_lead_list(active[active['days_since'] <= 30])
        warm = to_lead_list(active[(active['days_since'] > 30) & (active['days_since'] <= 90)])
        cold = to_lead_list(active[active['days_since'] > 90])

    results['hot_leads'] = {'count': len(hot), 'items': hot[:30]}
    results['warm_leads'] = {'count': len(warm), 'items': warm[:30]}
    results['cold_leads'] = {'count': len(cold), 'items': cold[:30]}

    # ── 8. Marketing Source Metrics & ROI ────────────────────────────────────
    meta_spend_total = 0
    tiktok_spend_total = 0
    if not shops_df.empty:
        meta_spend_total = shops_df['meta_spend_num'].sum()
        tiktok_spend_total = shops_df['tiktok_spend_num'].sum()

    revenue_by_class = {'meta': 0, 'tiktok': 0, 'twitter': 0, 'organic': 0, 'other': 0}
    leads_by_class   = {'meta': 0, 'tiktok': 0, 'twitter': 0, 'organic': 0, 'other': 0}
    conv_by_class    = {'meta': 0, 'tiktok': 0, 'twitter': 0, 'organic': 0, 'other': 0}

    # Build a combined first-touch frame (leads_df + wa_df).
    # For phones in both sheets keep the EARLIEST contact date so the source
    # that originally brought the lead gets the attribution credit.
    roi_parts = []
    if not leads_df.empty and 'phone_norm' in leads_df.columns and 'source_class' in leads_df.columns:
        roi_parts.append(leads_df[['phone_norm', 'date_parsed', 'source_class']].copy())
    if not wa_df.empty and 'phone_norm' in wa_df.columns and 'source_class' in wa_df.columns:
        roi_parts.append(wa_df[['phone_norm', 'date_parsed', 'source_class']].copy())

    if roi_parts:
        roi_combined = pd.concat(roi_parts, ignore_index=True)
        roi_combined = roi_combined[roi_combined['phone_norm'] != '']
        # First-touch: sort by date ascending, keep first row per phone
        with_date    = roi_combined[roi_combined['date_parsed'].notna()].sort_values('date_parsed').drop_duplicates('phone_norm')
        no_date      = roi_combined[roi_combined['date_parsed'].isna()].drop_duplicates('phone_norm')
        no_date      = no_date[~no_date['phone_norm'].isin(with_date['phone_norm'])]
        roi_df       = pd.concat([with_date, no_date], ignore_index=True)

        for cls, grp in roi_df.groupby('source_class'):
            if cls in leads_by_class:
                leads_by_class[cls] = len(grp)
                phones = set(grp['phone_norm'].dropna().unique())
                conv   = phones & converted_phones
                conv_by_class[cls] = len(conv)
                if not shops_df.empty and 'price_num' in shops_df.columns:
                    rev = shops_df[shops_df['phone_norm'].isin(conv)]['price_num'].sum()
                    revenue_by_class[cls] = float(rev)

    spend = {'meta': float(meta_spend_total), 'tiktok': float(tiktok_spend_total),
             'twitter': 0, 'organic': 0, 'other': 0}

    source_roi = {}
    for cls in ['meta', 'tiktok', 'twitter', 'organic', 'other']:
        s = spend[cls]
        rev = revenue_by_class[cls]
        roi = round((rev - s) / s * 100, 1) if s > 0 else None
        cpl = round(s / leads_by_class[cls], 2) if leads_by_class[cls] > 0 and s > 0 else None
        source_roi[cls] = {
            'leads': leads_by_class[cls],
            'conversions': conv_by_class[cls],
            'spend': s,
            'revenue': rev,
            'roi': roi,
            'cpl': cpl,
            'conv_rate': round(conv_by_class[cls] / leads_by_class[cls] * 100, 1)
                if leads_by_class[cls] > 0 else 0
        }
    results['source_roi'] = source_roi

    if not wa_df.empty and 'source' in wa_df.columns:
        wa_src = wa_df.groupby('source').agg(
            count=('source', 'count'),
            activity_sample=('activity', lambda x: x.mode()[0] if len(x) > 0 else '')
        ).reset_index().to_dict('records')
        results['wa_source_activity'] = wa_src
    else:
        results['wa_source_activity'] = []

    if not shops_df.empty and 'date_parsed' in shops_df.columns:
        shops_df['month'] = shops_df['date_parsed'].apply(
            lambda d: d.strftime('%Y-%m') if pd.notnull(d) and d else None)
        monthly = shops_df.dropna(subset=['month']).groupby('month').agg(
            conversions=('shop', 'count'),
            revenue=('price_num', 'sum')
        ).reset_index().to_dict('records')
        results['monthly_conversions'] = monthly
    else:
        results['monthly_conversions'] = []

    if not shops_df.empty and 'product' in shops_df.columns:
        prod = shops_df['product'].str.strip().value_counts().head(10).to_dict()
        results['top_products'] = prod
    else:
        results['top_products'] = {}

    if not shops_df.empty and 'gender' in shops_df.columns:
        results['gender_split'] = shops_df['gender'].str.strip().str.title().value_counts().to_dict()
    else:
        results['gender_split'] = {}

    # ── 10. Top 10 Customers by Branch ────────────────────────────────────────
    top_customers_by_branch = {}
    if not shops_df.empty and 'location' in shops_df.columns and 'phone_norm' in shops_df.columns:

        # ── Name lookup: first non-empty name per phone across all sources
        name_lookup = {}
        for df, col in [(shops_df, 'first_name'), (leads_df, 'name'), (wa_df, 'name')]:
            if df.empty or 'phone_norm' not in df.columns or col not in df.columns:
                continue
            sub = df[df['phone_norm'].notna() & (df['phone_norm'] != '')].copy()
            sub[col] = sub[col].fillna('').str.strip()
            sub = sub[sub[col] != ''].drop_duplicates('phone_norm')
            for p, n in zip(sub['phone_norm'], sub[col]):
                if p not in name_lookup:
                    name_lookup[p] = n

        # ── Interaction count per phone (leads + WA combined)
        interaction_counts = {}
        for df in (leads_df, wa_df):
            if df.empty or 'phone_norm' not in df.columns:
                continue
            for p, cnt in df[df['phone_norm'] != '']['phone_norm'].value_counts().items():
                interaction_counts[p] = interaction_counts.get(p, 0) + int(cnt)

        # ── First / last seen per phone across all sources
        date_parts = []
        for df in (leads_df, wa_df, shops_df):
            if df.empty or 'phone_norm' not in df.columns or 'date_parsed' not in df.columns:
                continue
            tmp = df[df['phone_norm'].notna() & (df['phone_norm'] != '') &
                     df['date_parsed'].notna()][['phone_norm', 'date_parsed']].copy()
            date_parts.append(tmp)

        date_lookup = {}
        if date_parts:
            comb = pd.concat(date_parts, ignore_index=True)
            dg = comb.groupby('phone_norm')['date_parsed'].agg(['min', 'max'])
            for phone, row in dg.iterrows():
                date_lookup[phone] = {
                    'first': str(row['min'].date()),
                    'last':  str(row['max'].date()),
                }

        # ── Top 10 per branch by total spend — converted leads only
        # (phone must appear in leads_2025 or whatsapp sheet AND in shops)
        valid = shops_df[
            (shops_df['phone_norm'] != '') &
            (shops_df['phone_norm'].isin(matched_converted))
        ].copy()
        valid['location'] = valid['location'].fillna('').str.strip().replace('', 'Unknown')
        for branch, grp in valid.groupby('location'):
            agg = (grp.groupby('phone_norm')
                   .agg(total_spend=('price_num', 'sum'), purchases=('price_num', 'count'))
                   .reset_index()
                   .sort_values('total_spend', ascending=False)
                   .head(10))
            top_list = []
            for _, r in agg.iterrows():
                p  = r['phone_norm']
                ds = date_lookup.get(p, {})
                top_list.append({
                    'name':         name_lookup.get(p, ''),
                    'phone':        p,
                    'interactions': interaction_counts.get(p, 0),
                    'purchases':    int(r['purchases']),
                    'total_spend':  round(float(r['total_spend']), 2),
                    'first_seen':   ds.get('first', ''),
                    'last_seen':    ds.get('last', ''),
                })
            top_customers_by_branch[branch] = top_list

    results['top_customers_by_branch'] = top_customers_by_branch

    return results


# ── Cache with background refresh ────────────────────────────────────────────
_cache = {'data': None, 'ts': None, 'shops': None, 'leads': None, 'wa': None}


def _refresh_cache():
    try:
        shops_df, leads_df, wa_df = load_data()

        # Process each frame exactly ONCE using unique-value deduplication.
        # compute_analytics() will skip re-processing when these columns exist.
        if not leads_df.empty:
            leads_df = _process_df(leads_df, 'contact', 'date', 'source')
        if not wa_df.empty:
            wa_df = _process_df(wa_df, 'contact', 'date', 'source')
        if not shops_df.empty:
            shops_df = _process_df(shops_df, 'phone', 'date', 'source') \
                if 'source' in shops_df.columns \
                else shops_df.copy()
            shops_df['date_parsed'] = shops_df['date'].map(
                {d: safe_date(d) for d in shops_df['date'].unique()}
            ) if 'date_parsed' not in shops_df.columns else shops_df['date_parsed']
            shops_df['phone_norm'] = shops_df['phone'].map(
                {p: normalize_phone(p) for p in shops_df['phone'].unique()}
            ).fillna('') if 'phone_norm' not in shops_df.columns else shops_df['phone_norm']
            try:
                shops_df['price_num'] = pd.to_numeric(
                    shops_df['price'].str.replace(',', ''), errors='coerce').fillna(0)
                shops_df['meta_spend_num'] = pd.to_numeric(
                    shops_df['meta_spend'].str.replace(',', ''), errors='coerce').fillna(0)
                shops_df['tiktok_spend_num'] = pd.to_numeric(
                    shops_df['tiktok_spend'].str.replace(',', ''), errors='coerce').fillna(0)
            except Exception:
                shops_df['price_num'] = 0.0
                shops_df['meta_spend_num'] = 0.0
                shops_df['tiktok_spend_num'] = 0.0

        data = compute_analytics(shops_df, leads_df, wa_df)
        _cache['data']  = data
        _cache['shops'] = shops_df
        _cache['leads'] = leads_df
        _cache['wa']    = wa_df
        _cache['ts']    = datetime.now()
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"Cache refresh error: {e}")


def get_analytics(force=False):
    if force or _cache['data'] is None:
        _refresh_cache()
        return _cache['data']

    # Return cached data immediately; trigger background refresh if stale
    age = (datetime.now() - _cache['ts']).seconds
    if age >= 300:
        threading.Thread(target=_refresh_cache, daemon=True).start()

    return _cache['data']


# Pre-warm cache so the first browser request is instant
threading.Thread(target=_refresh_cache, daemon=True).start()


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/api/analytics')
def api_analytics():
    try:
        data = get_analytics()
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/refresh')
def api_refresh():
    try:
        data = get_analytics(force=True)
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/customer')
def api_customer():
    phone_raw = request.args.get('phone', '').strip()
    if not phone_raw:
        return jsonify({'success': False, 'error': 'Phone number required'}), 400

    phone = normalize_phone(phone_raw)
    if not phone:
        return jsonify({'success': False, 'error': 'Invalid phone number'}), 400

    shops_df = _cache.get('shops')
    leads_df = _cache.get('leads')
    wa_df    = _cache.get('wa')

    if shops_df is None:
        return jsonify({'success': False, 'error': 'Data not loaded yet, try again shortly'}), 503

    # ── Purchases ────────────────────────────────────────────────────────────
    purchases = []
    lifetime_value = 0.0
    if not shops_df.empty and 'phone_norm' in shops_df.columns:
        p = shops_df[shops_df['phone_norm'] == phone].sort_values('date_parsed')
        for _, row in p.iterrows():
            dp  = row.get('date_parsed')
            amt = float(row.get('price_num', 0) or 0)
            lifetime_value += amt
            purchases.append({
                'date':     str(dp.date()) if pd.notna(dp) else '',
                'amount':   amt,
                'product':  str(row.get('product',  '') or '').strip(),
                'location': str(row.get('location', '') or row.get('shop', '') or '').strip(),
            })

    total_purchases = len(purchases)
    avg_spend = round(lifetime_value / total_purchases, 2) if total_purchases else 0

    # ── Interactions ─────────────────────────────────────────────────────────
    interactions = []
    name = ''

    if not leads_df.empty and 'phone_norm' in leads_df.columns:
        for _, row in leads_df[leads_df['phone_norm'] == phone].iterrows():
            dp = row.get('date_parsed')
            n  = str(row.get('name', '') or '').strip()
            if n and not name:
                name = n
            interactions.append({
                'date':     str(dp.date()) if pd.notna(dp) else '',
                'source':   str(row.get('source',       '') or '').strip(),
                'channel':  str(row.get('source_class', '') or '').strip(),
                'activity': 'Lead enquiry',
                'branch':   str(row.get('branch', '') or '').strip(),
            })

    if not wa_df.empty and 'phone_norm' in wa_df.columns:
        for _, row in wa_df[wa_df['phone_norm'] == phone].iterrows():
            dp = row.get('date_parsed')
            n  = str(row.get('name', '') or '').strip()
            if n and not name:
                name = n
            interactions.append({
                'date':     str(dp.date()) if pd.notna(dp) else '',
                'source':   str(row.get('source',       '') or '').strip(),
                'channel':  str(row.get('source_class', '') or '').strip(),
                'activity': str(row.get('activity',     '') or '').strip() or '—',
                'branch':   str(row.get('branch', '') or '').strip(),
            })

    interactions.sort(key=lambda x: x['date'] or '0000-00-00')

    if not purchases and not interactions:
        return jsonify({'success': True, 'found': False, 'phone': phone})

    # ── Summary stats ────────────────────────────────────────────────────────
    all_dates   = [x['date'] for x in interactions + purchases if x.get('date')]
    purch_dates = [p['date'] for p in purchases if p.get('date')]
    first_interaction = min(all_dates)   if all_dates   else ''
    first_purchase    = min(purch_dates) if purch_dates else ''

    today = datetime.now().date()
    days_as_customer = 0
    if first_interaction:
        from datetime import date as _date
        days_as_customer = (today - _date.fromisoformat(first_interaction)).days

    return jsonify({
        'success': True,
        'found':   True,
        'phone':   phone,
        'name':    name,
        'summary': {
            'lifetime_value':     round(lifetime_value, 2),
            'avg_spend':          avg_spend,
            'total_purchases':    total_purchases,
            'total_interactions': len(interactions),
            'days_as_customer':   days_as_customer,
            'first_interaction':  first_interaction,
            'first_purchase':     first_purchase,
        },
        'purchases':    purchases,
        'interactions': interactions,
    })


@app.route('/')
def index():
    html = open(_HTML_PATH, encoding='utf-8').read()
    return render_template_string(html)


_HTML_PATH = os.path.join(os.path.dirname(__file__), 'index.html')

if __name__ == '__main__':
    print("Customer Journey Analytics Dashboard")
    print("   http://localhost:5004")
    app.run(debug=True, port=5004)
