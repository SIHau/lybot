import os
import re
import sys
import asyncio
import sqlite3
from datetime import datetime
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    print("❌ 錯誤：未在 .env 檔案中找到 DISCORD_TOKEN，請先設定。")
    sys.exit(1)

# ---------- SQLite DB 設定 ----------
DB_PATH = os.path.join(os.path.dirname(__file__), "reports.db")

def init_db():
    """初始化資料庫表格（僅在啟動時執行一次）"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_time TEXT,
            user TEXT,
            keyword TEXT,
            proposal_id TEXT,
            status TEXT
        )""")

def log_report(user: str, keyword: str, proposal_id: str = None, status: str = None):
    """寫入查詢紀錄"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO reports (event_time, user, keyword, proposal_id, status) VALUES (?,?,?,?,?)",
            (datetime.now().isoformat(), user, keyword, proposal_id or "", status or ""),
        )

LY_API_BASE = "https://ly.govapi.tw/v2"
CURRENT_TERM = 11
CACHED_ALL_LEGISLATORS: list[dict] = []
CACHED_CURRENT_LEGISLATORS: list[dict] = []
CACHED_COMMITTEES: list[dict] = []  # [{"code": 26, "name": "社會福利及衛生環境委員會", "old": False}, ...]


def pick(d: dict, *keys, default=None):
    """依序嘗試多個欄位名稱（LYAPI v2 使用中文欄位，保留英文欄位作為相容）"""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def join_names(value, default: str) -> str:
    """把 list / dict / str 轉成可顯示的字串，避免印出 Python list 原始格式"""
    if isinstance(value, list):
        names = [str(v.get("name", v) if isinstance(v, dict) else v).strip() for v in value if v]
        return "、".join(n for n in names if n) or default
    return str(value).strip() or default if value else default


def extract_list(data, *keys) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        v = pick(data, *keys, default=[])
        return v if isinstance(v, list) else []
    return []


class LyBot(commands.Bot):
    async def setup_hook(self):
        # setup_hook 只會執行一次；on_ready 在斷線重連時會重複觸發
        init_db()
        await load_legislators_cache()
        await load_committees_cache()
        await self.tree.sync()


intents = discord.Intents.default()
bot = LyBot(command_prefix="!", intents=intents)


# ==========================================
# UI 元件：提案清單分頁控制 View
# ==========================================
class ProposalPaginationView(discord.ui.View):
    def __init__(self, proposals: list, keyword: str):
        super().__init__(timeout=120)
        self.proposals = proposals
        self.keyword = keyword
        self.current_page = 0
        self.total_pages = len(proposals)
        self.message: discord.Message | None = None
        self.update_buttons()

    def update_buttons(self):
        self.prev_btn.disabled = (self.current_page == 0)
        self.next_btn.disabled = (self.current_page >= self.total_pages - 1)

    def create_embed(self) -> discord.Embed:
        item = self.proposals[self.current_page]

        bill_name = pick(item, "議案名稱", "案由", "billName", "title", default="無案由說明")
        bill_no = pick(item, "議案編號", "billNo", default="未提供")
        proposers = pick(item, "提案人", "提案單位/提案委員", "proposers", "proposer", default=[])
        proposer_str = join_names(proposers, "未提供")
        status = pick(item, "議案狀態", "status", default="審議中")
        category = pick(item, "議案類別", default="未提供")
        progress_date = pick(item, "最新進度日期", default="未提供")
        detail_url = pick(item, "url")

        embed = discord.Embed(
            title=f"📜 提案查詢結果：{self.keyword}",
            color=discord.Color.teal(),
            url=detail_url if isinstance(detail_url, str) and detail_url.startswith("http") else None
        )
        embed.add_field(name="案由", value=str(bill_name)[:1000], inline=False)
        embed.add_field(name="議案編號", value=str(bill_no), inline=True)
        embed.add_field(name="提案人/機關", value=str(proposer_str)[:200], inline=True)
        embed.add_field(name="目前狀態", value=str(status), inline=True)
        embed.add_field(name="議案類別", value=str(category), inline=True)
        embed.add_field(name="最新進度日期", value=str(progress_date), inline=True)
        embed.set_footer(text=f"第 {self.current_page + 1} 頁 / 共 {self.total_pages} 頁")
        return embed

    @discord.ui.button(label="◀ 上一頁", style=discord.ButtonStyle.primary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="下一頁 ▶", style=discord.ButtonStyle.primary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def build_legislator_embed(item: dict, title: str, color: discord.Color) -> discord.Embed:
    embed = discord.Embed(title=f"👤 {item['name']} 委員資訊（{title}）", color=color)
    term = item.get("term")
    embed.add_field(name="屆期", value=f"第 {term} 屆" if term else "未提供", inline=True)
    embed.add_field(name="政黨", value=item["party"], inline=True)
    embed.add_field(name="所屬選區", value=item["areaName"], inline=False)
    if item.get("resigned"):
        embed.add_field(name="任職狀態", value="已離職", inline=True)
    embed.add_field(name="委員會紀錄", value=join_names(item["committee"], "院會 / 待分派")[:1000], inline=False)
    pic_url = item.get("picUrl")
    if isinstance(pic_url, str) and pic_url.startswith("http"):
        embed.set_thumbnail(url=pic_url)
    return embed


# ==========================================
# UI 元件：立委歷屆/多結果分頁 View
# ==========================================
class LegislatorPaginationView(discord.ui.View):
    def __init__(self, records: list, title: str):
        super().__init__(timeout=120)
        self.records = records
        self.title = title
        self.current_page = 0
        self.total_pages = len(records)
        self.message: discord.Message | None = None
        self.update_buttons()

    def update_buttons(self):
        self.prev_btn.disabled = (self.current_page == 0)
        self.next_btn.disabled = (self.current_page >= self.total_pages - 1)

    def create_embed(self) -> discord.Embed:
        embed = build_legislator_embed(self.records[self.current_page], self.title, discord.Color.purple())
        embed.set_footer(text=f"第 {self.current_page + 1} 筆 / 共 {self.total_pages} 筆資料")
        return embed

    @discord.ui.button(label="◀ 上一筆", style=discord.ButtonStyle.primary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="下一筆 ▶", style=discord.ButtonStyle.primary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ==========================================
# UI 元件：名單列表分頁 View（每頁多筆）
# ==========================================
class ListPaginationView(discord.ui.View):
    def __init__(self, lines: list[str], title: str, header: str = "", per_page: int = 20):
        super().__init__(timeout=120)
        # 多行項目（例如議事錄影）之間空一行
        self.separator = "\n\n" if any("\n" in line for line in lines) else "\n"
        self.pages = [lines[i:i + per_page] for i in range(0, len(lines), per_page)] or [[]]
        self.title = title
        self.header = header
        self.current_page = 0
        self.total_pages = len(self.pages)
        self.message: discord.Message | None = None
        self.update_buttons()

    def update_buttons(self):
        self.prev_btn.disabled = (self.current_page == 0)
        self.next_btn.disabled = (self.current_page >= self.total_pages - 1)

    def create_embed(self) -> discord.Embed:
        body = self.separator.join(self.pages[self.current_page])
        description = f"{self.header}\n\n{body}" if self.header else body
        embed = discord.Embed(title=self.title, description=description[:4000], color=discord.Color.gold())
        embed.set_footer(text=f"第 {self.current_page + 1} 頁 / 共 {self.total_pages} 頁")
        return embed

    @discord.ui.button(label="◀ 上一頁", style=discord.ButtonStyle.primary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="下一頁 ▶", style=discord.ButtonStyle.primary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ==========================================
# LYAPI 資料載入模組
# ==========================================
async def fetch_legislators_page(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    page: int = 1,
    limit: int = 100
) -> tuple[list, int, int | None]:
    """抓取單頁立委清單，回傳 (解析後資料, 原始筆數, 總頁數)；失敗時解析後資料為 None"""
    url = f"{LY_API_BASE}/legislators"
    params = {"page": page, "limit": limit}
    max_attempts = 5

    for attempt in range(1, max_attempts + 1):
        retry_after = None
        async with sem:
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        # 被限流或伺服器暫時錯誤：依 Retry-After（或指數退避）等待後重試
                        try:
                            retry_after = float(resp.headers.get("Retry-After", ""))
                        except ValueError:
                            retry_after = 2 ** attempt
                    elif resp.status != 200:
                        print(f"⚠️ 第 {page} 頁請求失敗: HTTP {resp.status}")
                        return None, 0, None
                    else:
                        return parse_legislators_response(await resp.json(content_type=None))
            except asyncio.TimeoutError:
                print(f"⚠️ 第 {page} 頁請求逾時 (Timeout)，第 {attempt} 次")
                retry_after = 2 ** attempt
            except Exception as e:
                print(f"⚠️ 載入第 {page} 頁時發生異常: {type(e).__name__} - {e}")
                return None, 0, None
            finally:
                await asyncio.sleep(0.5)  # 每個請求之間稍作間隔，避免觸發限流

        if attempt < max_attempts:
            await asyncio.sleep(min(retry_after, 30))

    print(f"⚠️ 第 {page} 頁重試 {max_attempts} 次仍失敗")
    return None, 0, None


def parse_legislators_response(data) -> tuple[list, int, int | None]:
    raw_list = extract_list(data, "legislators", "data", "records", "results")
    total_page = data.get("total_page") if isinstance(data, dict) else None

    parsed = []
    for r in raw_list:
        if not isinstance(r, dict):
            continue
        name = pick(r, "委員姓名", "name", "mFName", "legislator_name")
        if not name:
            continue

        try:
            term_val = int(pick(r, "屆", "term"))
        except (ValueError, TypeError):
            term_val = None  # 屆期不明就不要假裝是現任

        parsed.append({
            "name": str(name).strip(),
            "term": term_val,
            "party": str(pick(r, "黨籍", "party", "partyGroup", default="無黨籍")),
            "areaName": str(pick(r, "選區名稱", "areaName", "district", "zone", default="全國不分區")),
            "committee": pick(r, "委員會", "committee", "committees", default="院會 / 待分派"),
            "picUrl": pick(r, "照片位址", "picUrl", "image", "avatar", "pic"),
            "resigned": pick(r, "是否離職", default="否") == "是",
        })
    return parsed, len(raw_list), total_page


async def load_legislators_cache():
    """安全分批載入全體立委名冊至快取"""
    global CACHED_ALL_LEGISLATORS, CACHED_CURRENT_LEGISLATORS

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*"
    }

    # 關閉嚴格 SSL 驗證，避免 Python 本機環境缺少證書時報錯
    connector = aiohttp.TCPConnector(ssl=False)
    sem = asyncio.Semaphore(2)  # 最多同時 2 個請求；API 會對過快的請求回 HTTP 429
    limit = 100

    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        # 1. 取得第一頁資料
        first_page, raw_count, total_page = await fetch_legislators_page(session, sem, page=1, limit=limit)

        if first_page is None:
            print("❌ 無法取得立委名單，請確認網路連線或 API 伺服器狀態。")
            return

        all_records = list(first_page)
        failed_pages = []
        # API 會回傳 total_page（依 limit 計算，1656 筆 / 100 = 17 頁）
        max_pages = min(int(total_page), 50) if total_page else 25

        # 2. 有 total_page 就照它抓；沒有的話以「原始」筆數判斷是否還有下一頁
        if (total_page or raw_count >= limit) and max_pages > 1:
            current_page = 2
            while current_page <= max_pages:
                batch_pages = list(range(current_page, min(current_page + 5, max_pages + 1)))
                results = await asyncio.gather(
                    *[fetch_legislators_page(session, sem, page=p, limit=limit) for p in batch_pages]
                )

                last_page_reached = False
                for p, (p_data, p_raw, _) in zip(batch_pages, results):
                    if p_data is None:
                        # 請求失敗不代表沒資料了，記下來但繼續抓後面的頁
                        failed_pages.append(p)
                        continue
                    all_records.extend(p_data)
                    if p_raw == 0 or (not total_page and p_raw < limit):
                        last_page_reached = True

                if last_page_reached:
                    break
                current_page += 5

        CACHED_ALL_LEGISLATORS = all_records
        # 現任 = 第 11 屆且未離職
        CACHED_CURRENT_LEGISLATORS = [
            r for r in all_records if r.get("term") == CURRENT_TERM and not r.get("resigned")
        ]

        if failed_pages:
            print(f"⚠️ [LYAPI] 以下頁面載入失敗，歷任資料可能不完整：{failed_pages}")
        print(f"✅ [LYAPI] 成功載入全體委員資料共 {len(CACHED_ALL_LEGISLATORS)} 筆（現任第 {CURRENT_TERM} 屆: {len(CACHED_CURRENT_LEGISLATORS)} 位）")


DISTRICT_DATA = [
    {"name": "臺北市 第一選區（北投、士林）", "legislator": "吳思瑤", "party": "民主進步黨"},
    {"name": "臺北市 第二選區（大同、士林）", "legislator": "王世堅", "party": "民主進步黨"},
    {"name": "臺北市 第三選區（中山、松山）", "legislator": "王鴻薇", "party": "中國國民黨"},
    {"name": "新北市 第一選區（淡水、林口、泰山等）", "legislator": "洪孟楷", "party": "中國國民黨"},
    {"name": "新北市 第七選區（板橋東區）", "legislator": "葉元之", "party": "中國國民黨"},
    {"name": "新北市 第八選區（中和）", "legislator": "張智倫", "party": "中國國民黨"},
    {"name": "新竹市 選區（全區）", "legislator": "鄭正鈐", "party": "中國國民黨"},
    {"name": "新竹縣 第一選區（竹北西區、新豐、湖口等）", "legislator": "徐欣瑩", "party": "中國國民黨"},
    {"name": "新竹縣 第二選區（竹東、寶山、竹北東區等）", "legislator": "林思銘", "party": "中國國民黨"},
    {"name": "臺中市 第二選區（沙鹿、龍井、大肚、烏日、霧峰）", "legislator": "顏寬恒", "party": "中國國民黨"},
    {"name": "臺中市 第四選區（西屯、南屯）", "legislator": "廖偉翔", "party": "中國國民黨"},
    {"name": "高雄市 第六選區（鼓山、鹽埕、前金、新興、苓雅）", "legislator": "黃捷", "party": "民主進步黨"},
    {"name": "高雄市 第八選區（前鎮、小港、旗津）", "legislator": "賴瑞隆", "party": "民主進步黨"},
]

async def district_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=d["name"], value=d["name"])
        for d in DISTRICT_DATA
        if current.lower() in d["name"].lower()
    ][:25]


@bot.event
async def on_ready():
    print(f"🚀 機器人已成功啟動！登入身分：{bot.user}")


# ==========================================
# 指令 1：查詢立委（/立委 姓名、/立委 黨籍）
# ==========================================
legislator_group = app_commands.Group(name="立委", description="查詢立法委員資料")


@legislator_group.command(name="姓名", description="依姓名查詢現任或歷任立法委員基本資料、選區與所屬委員會")
@app_commands.describe(
    name="請輸入立法委員姓名（例如：黃國昌、王金平、柯建銘）",
    status="請選擇查詢現任或歷任委員（預設為現任）"
)
@app_commands.choices(
    status=[
        app_commands.Choice(name="現任委員 (第11屆)", value="current"),
        app_commands.Choice(name="歷任委員 (歷屆全部 1~11屆)", value="all")
    ]
)
async def query_legislator(
    interaction: discord.Interaction, 
    name: str, 
    status: app_commands.Choice[str] = None
):
    await interaction.response.defer()

    mode = status.value if status else "current"
    search_name = name.strip()

    if not CACHED_ALL_LEGISLATORS:
        await load_legislators_cache()

    source_data = CACHED_CURRENT_LEGISLATORS if mode == "current" else CACHED_ALL_LEGISLATORS
    mode_text = "現任（第 11 屆）" if mode == "current" else "歷任"

    matched = [leg for leg in source_data if search_name in leg.get("name", "")]

    if not matched:
        await interaction.followup.send(f"❌ 在 **{mode_text}** 名單中找不到名為 **{name}** 的委員資料。")
        return

    # 依屆期由新到舊排序
    matched.sort(key=lambda x: x.get("term") or 0, reverse=True)

    if len(matched) == 1:
        color = discord.Color.blue() if mode == "current" else discord.Color.purple()
        embed = build_legislator_embed(matched[0], mode_text, color)
        await interaction.followup.send(embed=embed)
    else:
        view = LegislatorPaginationView(records=matched, title=f"查詢結果: {search_name}")
        msg = await interaction.followup.send(embed=view.create_embed(), view=view)
        view.message = msg


# 常見簡稱 → API 上的正式黨名
PARTY_ALIASES = {
    "民進黨": "民主進步黨",
    "國民黨": "中國國民黨",
    "民眾黨": "台灣民眾黨",
    "時力": "時代力量",
    "基進": "台灣基進",
    "台聯": "台灣團結聯盟",
}

MOURNING_TEXT = "🕯️ 他們本屆不在國會裡面，讓我們一起為他們默哀"


def resolve_party(text: str) -> str:
    text = (text or "").strip()
    return PARTY_ALIASES.get(text, text)


def party_matches(record: dict, party: str) -> bool:
    return record.get("party") == party


def filter_by_party(party: str, term: int | None = None, area: str | None = None) -> list[dict]:
    """第一層黨籍（必填）→ 第二層屆數（選填）→ 第三層選區（選填，可輸入部分名稱，例如「臺北市」）"""
    area = (area or "").strip()
    return [
        r for r in CACHED_ALL_LEGISLATORS
        if party_matches(r, party)
        and (term is None or r.get("term") == term)
        and (not area or area in r.get("areaName", ""))
    ]


async def party_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    current = current.strip()
    target = resolve_party(current)
    counts: dict[str, int] = {}
    for r in CACHED_ALL_LEGISLATORS:
        counts[r["party"]] = counts.get(r["party"], 0) + 1
    parties = sorted(counts, key=lambda p: -counts[p])
    return [
        app_commands.Choice(name=p, value=p)
        for p in parties
        if not current or current in p or target in p
    ][:25]


async def term_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    party = resolve_party(getattr(interaction.namespace, "party", "") or "")
    records = filter_by_party(party) if party else CACHED_ALL_LEGISLATORS
    terms = sorted({r["term"] for r in records if r.get("term")}, reverse=True)
    return [
        app_commands.Choice(name=f"第 {t} 屆", value=t)
        for t in terms
        if not current or str(current).strip() in str(t)
    ][:25]


async def area_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    party = resolve_party(getattr(interaction.namespace, "party", "") or "")
    term = getattr(interaction.namespace, "term", None)
    try:
        term = int(term) if term else None
    except (TypeError, ValueError):
        term = None
    records = filter_by_party(party, term) if party else CACHED_ALL_LEGISLATORS
    areas = sorted({r["areaName"] for r in records if r.get("areaName")})
    return [
        app_commands.Choice(name=a, value=a)
        for a in areas
        if current.strip() in a
    ][:25]


@legislator_group.command(name="黨籍", description="依黨籍查詢立法委員，可再依屆數與選區篩選")
@app_commands.describe(
    party="第一層：黨籍（可輸入簡稱，例如：民進黨、國民黨、民眾黨）",
    term="第二層：屆數（選填，不填則查詢歷屆）",
    area="第三層：選區（選填，例如：全國不分區、臺北市第1選舉區，輸入「臺北市」可查全部臺北市選區）"
)
@app_commands.autocomplete(party=party_autocomplete, term=term_autocomplete, area=area_autocomplete)
async def query_by_party(
    interaction: discord.Interaction,
    party: str,
    term: app_commands.Range[int, 1, 99] | None = None,
    area: str | None = None
):
    await interaction.response.defer()

    if not CACHED_ALL_LEGISLATORS:
        await load_legislators_cache()
    if not CACHED_ALL_LEGISLATORS:
        await interaction.followup.send("❌ 立委名冊尚未載入，請稍後再試。")
        return

    party_name = resolve_party(party)
    party_records = filter_by_party(party_name)
    if not party_records:
        await interaction.followup.send(f"❌ 歷屆立委名單中找不到黨籍 **{party}**，請從下拉選單選擇。")
        return

    # 最新一屆完全沒有該黨委員 → 默哀
    latest_term = max(r["term"] for r in CACHED_ALL_LEGISLATORS if r.get("term"))
    mourning = not any(r.get("term") == latest_term for r in party_records)

    matched = filter_by_party(party_name, term, area)

    filters = [f"第 {term} 屆" if term else "歷屆"]
    if area:
        filters.append(area.strip())
    title = f"🏛️ {party_name} 立委名單（{'・'.join(filters)}）"

    if not matched:
        text = f"找不到符合條件的委員：**{party_name}**／{'／'.join(filters)}"
        if mourning:
            text = f"{MOURNING_TEXT}\n\n{text}"
        await interaction.followup.send(text)
        return

    matched.sort(key=lambda r: (-(r.get("term") or 0), r.get("areaName", ""), r["name"]))
    lines = [
        f"第 {r['term']} 屆｜**{r['name']}**｜{r['areaName']}{'（已離職）' if r.get('resigned') else ''}"
        for r in matched
    ]
    header = f"共 {len(matched)} 筆"
    if mourning:
        header = f"{MOURNING_TEXT}\n\n{header}"

    view = ListPaginationView(lines, title=title, header=header)
    msg = await interaction.followup.send(embed=view.create_embed(), view=view)
    view.message = msg


bot.tree.add_command(legislator_group)


# ==========================================
# 指令 2：依選區查詢
# ==========================================
@bot.tree.command(name="選區", description="依縣市或鄉鎮市區關鍵字查詢代表立委")
@app_commands.describe(district="輸入選區或鄉鎮關鍵字（例如：竹北、板橋）")
@app_commands.autocomplete(district=district_autocomplete)
async def query_district(interaction: discord.Interaction, district: str):
    target = next((d for d in DISTRICT_DATA if d["name"] == district), None)

    if not target:
        await interaction.response.send_message(
            "❌ 找不到該選區，請在輸入時直接從選單清單中選擇。",
            ephemeral=True
        )
        return

    embed = discord.Embed(title=f"📍 {target['name']}", color=discord.Color.green())
    embed.add_field(name="現任立委", value=target["legislator"], inline=True)
    embed.add_field(name="政黨", value=target["party"], inline=True)
    await interaction.response.send_message(embed=embed)


# ==========================================
# 指令 3：查詢提案
# ==========================================
@bot.tree.command(name="提案", description="查詢立法院最新相關法律提案與進度")
@app_commands.describe(keyword="輸入案由關鍵字或提案委員姓名")
async def query_proposals(interaction: discord.Interaction, keyword: str):
    await interaction.response.defer()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*"
    }
    connector = aiohttp.TCPConnector(ssl=False)
    url = f"{LY_API_BASE}/bills"

    # 關鍵字剛好是委員姓名 → 用 API 支援的「提案人」篩選；否則做全文搜尋
    kw = keyword.strip()
    by_proposer = any(leg["name"] == kw for leg in CACHED_ALL_LEGISLATORS)
    if by_proposer:
        params = {"提案人": kw, "limit": 10}
    else:
        # q 不加引號會逐字比對（幾乎所有議案都符合）；加上引號才是完整詞組搜尋
        phrase = kw.replace('"', "")
        params = {"q": f'"{phrase}"', "limit": 10}

    try:
        async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    await interaction.followup.send(f"❌ 查詢失敗，API 回傳狀態碼：`HTTP {resp.status}`")
                    return
                data = await resp.json(content_type=None)
    except Exception as e:
        await interaction.followup.send(f"❌ 連線 API 發生異常：`{type(e).__name__} - {e}`")
        return

    proposals = [p for p in extract_list(data, "bills", "data", "items") if isinstance(p, dict)]

    if not proposals:
        await interaction.followup.send(f"找不到與關鍵字「**{keyword}**」相關的提案。")
        return

    view = ProposalPaginationView(proposals=proposals, keyword=keyword)
    await asyncio.to_thread(log_report, user=interaction.user.name, keyword=keyword, status="success")
    msg = await interaction.followup.send(embed=view.create_embed(), view=view)
    view.message = msg


# ==========================================
# 指令 4：議事錄影 IVOD 搜尋
# ==========================================
API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*"
}


async def load_committees_cache():
    """載入委員會清單（約 18 筆），供 /議事錄影 的委員會選單使用"""
    global CACHED_COMMITTEES
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(headers=API_HEADERS, connector=connector) as session:
            async with session.get(f"{LY_API_BASE}/committees", params={"limit": 100},
                                   timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    print(f"⚠️ 委員會清單載入失敗: HTTP {resp.status}")
                    return
                data = await resp.json(content_type=None)
    except Exception as e:
        print(f"⚠️ 委員會清單載入失敗: {type(e).__name__} - {e}")
        return

    committees = []
    for c in extract_list(data, "committees"):
        code, name = c.get("委員會代號"), c.get("委員會名稱")
        if code is None or not name:
            continue
        committees.append({"code": int(code), "name": str(name), "old": c.get("委員會類別") == 3})
    # 現行委員會排前面，國會改革前的舊委員會排後面
    committees.sort(key=lambda c: (c["old"], c["code"]))
    CACHED_COMMITTEES = committees
    print(f"✅ [LYAPI] 成功載入委員會清單共 {len(committees)} 個")


def committee_label(c: dict) -> str:
    return f"{c['name']}（舊）" if c["old"] else c["name"]


def resolve_committee(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return next((c for c in CACHED_COMMITTEES if c["code"] == int(text)), None)
    return (next((c for c in CACHED_COMMITTEES if c["name"] == text), None)
            or next((c for c in CACHED_COMMITTEES if text in c["name"]), None))


async def committee_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    current = current.strip()
    return [
        app_commands.Choice(name=committee_label(c), value=str(c["code"]))
        for c in CACHED_COMMITTEES
        if current in c["name"]
    ][:25]


def parse_date(text: str) -> str | None:
    """接受 2026-08-27、2026/8/27、20260827，回傳 API 使用的 YYYY-MM-DD；格式錯誤回傳 None"""
    m = re.fullmatch(r"\s*(\d{4})[-/.]?(\d{1,2})[-/.]?(\d{1,2})\s*", text or "")
    if not m:
        return None
    try:
        return datetime(int(m[1]), int(m[2]), int(m[3])).strftime("%Y-%m-%d")
    except ValueError:
        return None


def format_duration(seconds) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "未知"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def ivod_text(item: dict) -> str:
    """議程比對用文字：會議名稱（含事由）與會議標題"""
    meeting = item.get("會議資料") or {}
    return f"{item.get('會議名稱', '')} {meeting.get('標題', '')}"


def format_ivod_line(item: dict) -> str:
    meeting = item.get("會議資料") or {}
    date = item.get("日期") or "日期不明"
    start = str(item.get("開始時間") or "")[11:16]
    committees = join_names(meeting.get("委員會代碼:str"), "") or meeting.get("種類") or ""
    speaker = item.get("委員名稱") or ""
    kind = "完整會議" if item.get("影片種類") == "Full" else f"🎤 {speaker}"
    name = str(item.get("會議名稱") or meeting.get("標題") or "未命名會議")
    if len(name) > 90:
        name = name[:90] + "…"
    url = item.get("IVOD_URL") or ""
    link = f"[▶ 觀看]({url})" if str(url).startswith("http") else ""
    return (
        f"**{date} {start}**｜{committees}｜{kind}｜{format_duration(item.get('影片長度'))}\n"
        f"{name}\n{link}"
    )


@bot.tree.command(name="議事錄影", description="搜尋立法院議事錄影（IVOD），可依關鍵字、日期、委員會、議程篩選")
@app_commands.describe(
    keyword="關鍵字：輸入委員姓名可找該委員的發言片段，其他文字做全文搜尋",
    date="日期（例如：2026-08-27、2026/8/27）",
    committee="委員會（從選單選擇）",
    agenda="議程：會議名稱或事由中的文字（例如：身心障礙者權益保障法）",
    video_type="影片種類（預設全部）"
)
@app_commands.choices(video_type=[
    app_commands.Choice(name="完整會議", value="Full"),
    app_commands.Choice(name="委員發言片段", value="Clip"),
])
@app_commands.autocomplete(committee=committee_autocomplete)
async def query_ivod(
    interaction: discord.Interaction,
    keyword: str | None = None,
    date: str | None = None,
    committee: str | None = None,
    agenda: str | None = None,
    video_type: app_commands.Choice[str] | None = None
):
    keyword = (keyword or "").replace('"', "").strip()
    agenda = (agenda or "").replace('"', "").strip()

    if not any([keyword, date, committee, agenda]):
        await interaction.response.send_message(
            "❌ 請至少填入一個條件：關鍵字、日期、委員會或議程。", ephemeral=True)
        return

    api_date = None
    if date:
        api_date = parse_date(date)
        if not api_date:
            await interaction.response.send_message(
                f"❌ 無法辨識日期「{date}」，請使用 2026-08-27 或 2026/8/27 的格式。", ephemeral=True)
            return

    comm = None
    if committee:
        comm = resolve_committee(committee)
        if not comm:
            await interaction.response.send_message(
                f"❌ 找不到委員會「{committee}」，請從下拉選單選擇。", ephemeral=True)
            return

    await interaction.response.defer()

    params: dict = {}
    if api_date:
        params["日期"] = api_date
    if comm:
        params["會議資料.委員會代碼"] = comm["code"]
    if video_type:
        params["影片種類"] = video_type.value

    # 關鍵字是委員姓名 → 用「委員名稱」篩選發言片段；其他關鍵字 → 全文詞組搜尋
    by_speaker = bool(keyword) and any(leg["name"] == keyword for leg in CACHED_ALL_LEGISLATORS)
    if by_speaker:
        params["委員名稱"] = keyword

    # 全文搜尋只能帶一個 q：優先用議程，關鍵字改在本地比對
    local_keyword = ""
    if agenda:
        params["q"] = f'"{agenda}"'
        if keyword and not by_speaker:
            local_keyword = keyword
    elif keyword and not by_speaker:
        params["q"] = f'"{keyword}"'

    needs_local = bool(agenda or local_keyword)
    params["limit"] = 50 if needs_local else 20

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(headers=API_HEADERS, connector=connector) as session:
            async with session.get(f"{LY_API_BASE}/ivods", params=params,
                                   timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    await interaction.followup.send(f"❌ 查詢失敗，API 回傳狀態碼：`HTTP {resp.status}`")
                    return
                data = await resp.json(content_type=None)
    except Exception as e:
        await interaction.followup.send(f"❌ 連線 API 發生異常：`{type(e).__name__} - {e}`")
        return

    items = [i for i in extract_list(data, "ivods") if isinstance(i, dict)]

    # API 沒有處理 q（回應裡沒有 query）時，關鍵字也改在本地比對
    if "q" in params and isinstance(data, dict) and "query" not in data and keyword and not by_speaker:
        local_keyword = keyword
    if agenda:
        items = [i for i in items if agenda in ivod_text(i)]
    if local_keyword:
        items = [i for i in items if local_keyword in f"{ivod_text(i)} {i.get('委員名稱', '')}"]

    filters = []
    if keyword:
        filters.append(f"關鍵字：{keyword}")
    if api_date:
        filters.append(f"日期：{api_date}")
    if comm:
        filters.append(f"委員會：{committee_label(comm)}")
    if agenda:
        filters.append(f"議程：{agenda}")
    if video_type:
        filters.append(f"種類：{video_type.name}")
    filter_text = "｜".join(filters)

    if not items:
        await interaction.followup.send(f"找不到符合條件的議事錄影（{filter_text}）。")
        return

    total = data.get("total") if isinstance(data, dict) and not needs_local else None
    header = filter_text + (f"\n共 {total} 筆，顯示最新 {len(items)} 筆" if total and total > len(items) else f"\n共 {len(items)} 筆")
    view = ListPaginationView([format_ivod_line(i) for i in items], title="🎬 議事錄影搜尋結果",
                              header=header, per_page=5)
    view_embed = view.create_embed()
    msg = await interaction.followup.send(embed=view_embed, view=view)
    view.message = msg
    await asyncio.to_thread(log_report, user=interaction.user.name, keyword=f"[IVOD] {filter_text}", status="success")


if __name__ == "__main__":
    bot.run(TOKEN)