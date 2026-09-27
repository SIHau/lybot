import os
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
# LYAPI 資料載入模組
# ==========================================
async def fetch_legislators_page(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    page: int = 1,
    limit: int = 100
) -> tuple[list, int, int | None]:
    """抓取單頁立委清單，回傳 (解析後資料, 原始筆數, 總頁數)"""
    url = f"{LY_API_BASE}/legislators"
    params = {"page": page, "limit": limit}

    async with sem:
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    print(f"⚠️ 第 {page} 頁請求失敗: HTTP {resp.status}")
                    return [], 0, None

                data = await resp.json(content_type=None)
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
        except asyncio.TimeoutError:
            print(f"⚠️ 第 {page} 頁請求逾時 (Timeout)")
        except Exception as e:
            print(f"⚠️ 載入第 {page} 頁時發生異常: {type(e).__name__} - {e}")
        return [], 0, None


async def load_legislators_cache():
    """安全分批載入全體立委名冊至快取"""
    global CACHED_ALL_LEGISLATORS, CACHED_CURRENT_LEGISLATORS

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*"
    }

    # 關閉嚴格 SSL 驗證，避免 Python 本機環境缺少證書時報錯
    connector = aiohttp.TCPConnector(ssl=False)
    sem = asyncio.Semaphore(4)  # 最多同時發送 4 個請求，防止觸發 API 限流
    limit = 100

    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        # 1. 取得第一頁資料
        first_page, raw_count, total_page = await fetch_legislators_page(session, sem, page=1, limit=limit)

        if not first_page:
            print("❌ 無法取得立委名單，請確認網路連線或 API 伺服器狀態。")
            return

        all_records = list(first_page)
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
                for p_data, p_raw, _ in results:
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
# 指令 1：查詢立委
# ==========================================
@bot.tree.command(name="立委", description="查詢現任或歷任立法委員基本資料、選區與所屬委員會")
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
    if any(leg["name"] == kw for leg in CACHED_ALL_LEGISLATORS):
        params = {"提案人": kw, "limit": 10}
    else:
        params = {"q": kw, "limit": 10}

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


if __name__ == "__main__":
    bot.run(TOKEN)