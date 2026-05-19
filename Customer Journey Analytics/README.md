# Customer Journey Analytics Dashboard

## Setup

1. **Place these files** in a folder on your Windows machine (e.g., `C:\Dashboard\`)

2. **Check the config** at the top of `app.py`:
   ```python
   JSON_FILE_PATH = r'C:\Users\Administrator\Downloads\retention-485013-974e48474123.json'
   SPREADSHEET_ID = '1zravAS7NoxjnV-2476eBhMitZYQmxWgef3JTbwD-Rag'
   ```
   These match your credentials — no changes needed unless the file moves.

3. **Share your Google Sheet** with the service account email from the JSON file
   (e.g., `something@retention-485013.iam.gserviceaccount.com`) — give it **Viewer** access.

4. **Run the dashboard** by double-clicking `START_DASHBOARD.bat`

5. **Open browser** → `http://localhost:5000`

---

## Sheet Structure Expected

| Sheet | Columns | Notes |
|-------|---------|-------|
| Any shop sheet (Hazina, Mombasa, etc.) | A=Date, B=First Name, C=Gender, D=Phone, E=Product, F=Color, G=Category, H=Location, I=Price, J=Meta Spend, K=TikTok Spend | Each shop is its own sheet |
| Leads_2025 | A=Date, B=Contact, C=Name, D=Branch, E=Source | |
| Whatsapp | A=Date, B=Name, C=Contact, D=Source, E=Activity, F=Branch | |

---

## Dashboard Tabs

1. **Overview** — KPI cards + monthly trends, region, gender, product charts
2. **Lead Sources** — All source breakdown, Meta/TikTok/Organic classification, WhatsApp activity
3. **Branches** — Leads vs conversions per branch with region grouping
4. **Journey** — Time from lead to conversion, distribution bars, matched records
5. **Lead Status** — 🔥 Hot (≤30d) / ☀️ Warm (≤90d) / ❄️ Cold (>90d) — excludes converted
6. **Marketing ROI** — Spend vs Revenue per channel, ROI %, Cost per Lead

---

## Source Classification Logic

| Category | Sources |
|----------|---------|
| **Meta Ads** | facebook, meta ad, meta ads, direct ig, instagram, ig, liz, meta ad x, meta ad fb, meta ad-ig, new direct |
| **TikTok Ads** | tik tok, tiktok |
| **Organic** (no spend) | e direct, web check out, website, existing customers |
| **Other** | anything else |

---

## Data Refresh
- Dashboard auto-refreshes every **5 minutes**
- Click **↺ REFRESH** button to force immediate update
- Data is cached for 5 minutes between requests to avoid API quota limits
