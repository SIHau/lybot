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
CACHED_ALL_LEGISLATORS: list[dict] = []
CACHED_CURRENT_LEGISLATORS: list[dict] = []

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


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

        bill_name = item.get("billName") or item.get("title") or "無案由說明"
        bill_no = item.get("billNo") or "未提供"

        proposers = item.get("proposers") or item.get("proposer") or []
        if isinstance(proposers, list):
            proposer_str = "、".join([str(p.get("name", p) if isinstance(p, dict) else p) for p in proposers if p]) or "未提供"
        else:
            proposer_str = str(proposers)

        status = item.get("status") or "審議中"

        embed = discord.Embed(
            title=f"📜 提案查詢結果：{self.keyword}",
            color=discord.Color.teal()
        )
        embed.add_field(name="案由", value=str(bill_name)[:1000], inline=False)
        embed.add_field(name="議案編號", value=str(bill_no), inline=True)
        embed.add_field(name="提案人/機關", value=str(proposer_str)[:200], inline=True)
        embed.add_field(name="目前狀態", value=str(status), inline=True)
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
        item = self.records[self.current_page]

        leg_name = item.get("name", "未知")
        term_num = item.get("term", "未提供")
        party = item.get("party") or item.get("partyGroup") or "無黨籍"
        area = item.get("areaName") or item.get("district") or item.get("zone") or "全國不分區"

        committee_raw = item.get("committee") or item.get("committees") or "院會 / 待分派"
        if isinstance(committee_raw, list):
            clean_comms = [str(c.get("name", c) if isinstance(c, dict) else c).strip() for c in committee_raw if c]
            committee_str = "、".join(clean_comms) if clean_comms else "院會 / 待分派"
        else:
            committee_str = str(committee_raw)

        pic_url = item.get("picUrl") or item.get("image") or item.get("avatar")

        embed = discord.Embed(
            title=f"👤 {leg_name} 委員資訊 ({self.title})",
            color=discord.Color.purple()
        )
        embed.add_field(name="屆期", value=f"第 {term_num} 屆", inline=True)
        embed.add_field(name="政黨", value=party, inline=True)
        embed.add_field(name="所屬選區", value=area, inline=False)
        embed.add_field(name="委員會紀錄", value=committee_str[:1000], inline=False)

        if pic_url:
            embed.set_thumbnail(url=pic_url)

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
) -> list:
    """從 API 抓取單頁立委清單（帶有並行保護與完整解析）"""
    url = f"{LY_API_BASE}/legislators"
    params = {"page": page, "limit": limit}

    async with sem:
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    print(f"⚠️ 第 {page} 頁請求失敗: HTTP {resp.status}")
                    return []

                data = await resp.json()

                raw_list = []
                if isinstance(data, dict):
                    raw_list = (
                        data.get("legislators")
                        or data.get("data")
                        or data.get("records")
                        or data.get("results")
                        or []
                    )
                elif isinstance(data, list):
                    raw_list = data

                parsed = []
                for r in raw_list:
                    name = r.get("name") or r.get("mFName") or r.get("legislator_name") or ""
                    if not name:
                        continue

                    term_val = r.get("term") or r.get("session") or 11
                    try:
                        term_val = int(term_val)
                    except (ValueError, TypeError):
                        term_val = 11

                    committee_raw = r.get("committee") or r.get("committees") or "院會 / 待分派"
                    pic_url = r.get("picUrl") or r.get("image") or r.get("avatar") or r.get("pic")

                    parsed.append({
                        "name": str(name).strip(),
                        "term": term_val,
                        "party": r.get("party") or r.get("partyGroup") or "無黨籍",
                        "areaName": r.get("areaName") or r.get("district") or r.get("zone") or "全國不分區",
                        "committee": committee_raw,
                        "picUrl": pic_url
                    })
                return parsed
        except asyncio.TimeoutError:
            print(f"⚠️ 第 {page} 頁請求逾時 (Timeout)")
        except Exception as e:
            print(f"⚠️ 載入第 {page} 頁時發生異常: {type(e).__name__} - {e}")
        return []


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

    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        # 1. 取得第一頁資料
        first_page = await fetch_legislators_page(session, sem, page=1, limit=100)

        if not first_page:
            print("❌ 無法取得立委名單，請確認網路連線或 API 伺服器狀態。")
            return

        all_records = list(first_page)
        
        # 2. 若第一頁筆數達到 100，以 5 頁為一組批次抓取後續資料
        if len(first_page) >= 100:
            current_page = 2
            max_pages = 25

            while current_page <= max_pages:
                batch_pages = list(range(current_page, min(current_page + 5, max_pages + 1)))
                tasks = [fetch_legislators_page(session, sem, page=p, limit=100) for p in batch_pages]
                results = await asyncio.gather(*tasks)

                empty_encountered = False
                for p_data in results:
                    if p_data:
                        all_records.extend(p_data)
                        if len(p_data) < 100:
                            empty_encountered = True
                    else:
                        empty_encountered = True

                if empty_encountered:
                    break

                current_page += 5

        CACHED_ALL_LEGISLATORS = all_records
        CACHED_CURRENT_LEGISLATORS = [r for r in all_records if r.get("term") == 11]

        print(f"✅ [LYAPI] 成功載入全體委員資料共 {len(CACHED_ALL_LEGISLATORS)} 筆（現任第 11 屆: {len(CACHED_CURRENT_LEGISLATORS)} 位）")


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
    init_db()
    await load_legislators_cache()
    await bot.tree.sync()
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
    matched.sort(key=lambda x: int(x.get("term", 0)), reverse=True)

    if len(matched) == 1:
        target = matched[0]
        embed = discord.Embed(
            title=f"👤 {target['name']} 委員資訊（{mode_text}）",
            color=discord.Color.blue() if mode == "current" else discord.Color.purple()
        )
        embed.add_field(name="屆期", value=f"第 {target['term']} 屆", inline=True)
        embed.add_field(name="政黨", value=target["party"], inline=True)
        embed.add_field(name="所屬選區", value=target["areaName"], inline=False)
        embed.add_field(name="委員會紀錄", value=str(target["committee"])[:1000], inline=False)

        if target.get("picUrl"):
            embed.set_thumbnail(url=target["picUrl"])

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

    try:
        async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
            async with session.get(url, params={"q": keyword.strip(), "limit": 10}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    await interaction.followup.send(f"❌ 查詢失敗，API 回傳狀態碼：`HTTP {resp.status}`")
                    return
                data = await resp.json()
    except Exception as e:
        await interaction.followup.send(f"❌ 連線 API 發生異常：`{type(e).__name__} - {e}`")
        return

    proposals = []
    if isinstance(data, dict):
        proposals = data.get("bills") or data.get("data") or data.get("items") or []
    elif isinstance(data, list):
        proposals = data

    if not proposals:
        await interaction.followup.send(f"找不到與關鍵字「**{keyword}**」相關的提案。")
        return

    view = ProposalPaginationView(proposals=proposals, keyword=keyword)
    await asyncio.to_thread(log_report, user=interaction.user.name, keyword=keyword, status="success")
    msg = await interaction.followup.send(embed=view.create_embed(), view=view)
    view.message = msg


if __name__ == "__main__":
    bot.run(TOKEN)