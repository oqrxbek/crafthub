import os
import asyncio
import logging
from datetime import datetime, timezone
from html import escape

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from openai import AsyncOpenAI
from sqlalchemy import String, Integer, Boolean, DateTime, Text, BigInteger, select, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("crafthub")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./crafthub.db")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
BASE_URL = os.getenv("BASE_URL", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
RUN_MODE = os.getenv("RUN_MODE", "polling")
BOT_USERNAME = os.getenv("BOT_USERNAME", "crafthubot")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me")
AI_DAILY_LIMIT = int(os.getenv("AI_DAILY_LIMIT", "50"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)
ai = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

class Base(DeclarativeBase):
    pass

def now():
    return datetime.now(timezone.utc)

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str|None] = mapped_column(String(255))
    first_name: Mapped[str|None] = mapped_column(String(255))
    banned: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_today: Mapped[int] = mapped_column(Integer, default=0)
    ai_date: Mapped[str|None] = mapped_column(String(20))
    coins: Mapped[int] = mapped_column(Integer, default=0)
    referral: Mapped[str] = mapped_column(String(100), unique=True)
    referred_by: Mapped[int|None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Channel(Base):
    __tablename__ = "channels"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(255), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    join_url: Mapped[str] = mapped_column(String(1000))
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class Server(Base):
    __tablename__ = "servers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=25565)
    version: Mapped[str|None] = mapped_column(String(100))
    mode: Mapped[str|None] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class AIMessage(Base):
    __tablename__ = "ai_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    role: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(255))
    details: Mapped[str|None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_user(tg):
    async with Session() as db:
        u = (await db.execute(select(User).where(User.telegram_id == tg.id))).scalar_one_or_none()
        if not u:
            import secrets
            u = User(
                telegram_id=tg.id, username=tg.username, first_name=tg.first_name,
                referral=secrets.token_urlsafe(8)
            )
            db.add(u)
        else:
            u.username, u.first_name = tg.username, tg.first_name
        await db.commit()
        return u

async def required_channels():
    async with Session() as db:
        return (await db.execute(select(Channel).where(Channel.active == True))).scalars().all()

async def subscribed(uid):
    from aiogram.enums import ChatMemberStatus
    missing = []
    for c in await required_channels():
        try:
            m = await bot.get_chat_member(c.chat_id, uid)
            if m.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
                missing.append(c)
        except Exception:
            missing.append(c)
    return missing

def sub_keyboard(channels):
    rows = [[InlineKeyboardButton(text=f"📢 {c.title}", url=c.join_url)] for c in channels]
    rows.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="sub_check")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 AI Assistant", callback_data="ai")],
        [InlineKeyboardButton(text="🎮 Servers", callback_data="servers"),
         InlineKeyboardButton(text="🗺️ Maps", callback_data="maps")],
        [InlineKeyboardButton(text="🧱 Builds", callback_data="builds"),
         InlineKeyboardButton(text="🧪 Commands", callback_data="commands")],
        [InlineKeyboardButton(text="🏆 Achievements", callback_data="achievements"),
         InlineKeyboardButton(text="👤 Profile", callback_data="profile")]
    ])

SYSTEM = """You are CraftHub AI, a professional Minecraft assistant.
Answer Minecraft questions about Java, Bedrock, commands, crafting,
enchantments, mobs, farms, redstone, biomes, structures, servers,
plugins, mods, datapacks, PvP, performance and troubleshooting.
You can answer general questions too. Prefer Uzbek when the user writes Uzbek.
Use concise practical answers. Put commands in code blocks.
If version matters, mention the relevant version or ask for it.
"""

async def ask_ai(uid, text):
    if not ai:
        return "⚠️ AI sozlanmagan. Render'da OPENAI_API_KEY qo‘yilishi kerak."
    async with Session() as db:
        u = (await db.execute(select(User).where(User.telegram_id == uid))).scalar_one()
        today = now().date().isoformat()
        if u.ai_date != today:
            u.ai_date, u.ai_today = today, 0
        if u.ai_today >= AI_DAILY_LIMIT:
            return f"⏳ Bugungi limit tugadi: {AI_DAILY_LIMIT} ta AI so‘rovi."
        u.ai_today += 1
        history = list(reversed((await db.execute(
            select(AIMessage).where(AIMessage.telegram_id == uid)
            .order_by(AIMessage.created_at.desc()).limit(10)
        )).scalars().all()))
        await db.commit()

    inp = [{"role": x.role, "content": x.text} for x in history]
    inp.append({"role": "user", "content": text})
    try:
        r = await ai.responses.create(
            model=OPENAI_MODEL, instructions=SYSTEM, input=inp,
            max_output_tokens=1200, safety_identifier=str(uid)
        )
        answer = (r.output_text or "Javob bo‘sh qaytdi.").strip()
        async with Session() as db:
            db.add(AIMessage(telegram_id=uid, role="user", text=text))
            db.add(AIMessage(telegram_id=uid, role="assistant", text=answer))
            await db.commit()
        return answer
    except Exception:
        log.exception("AI error")
        return "⚠️ AI servisida xatolik yuz berdi. Keyinroq qayta urinib ko‘ring."

@router.message(CommandStart())
async def start(m: Message):
    u = await get_user(m.from_user)
    if u.banned:
        await m.answer("🚫 Siz bloklangansiz.")
        return
    missing = await subscribed(m.from_user.id)
    if missing:
        await m.answer("🔒 CraftHub'dan foydalanish uchun kanallarga obuna bo‘ling:",
                       reply_markup=sub_keyboard(missing))
        return
    await m.answer(
        "⛏️ <b>CraftHub</b>ga xush kelibsiz!\n\n"
        "Minecraft AI, serverlar, commands, builds va boshqa funksiyalar bir joyda.",
        parse_mode="HTML", reply_markup=menu()
    )

@router.callback_query(F.data == "sub_check")
async def sub_check(c: CallbackQuery):
    missing = await subscribed(c.from_user.id)
    if missing:
        await c.answer("❌ Hali barcha kanallarga obuna bo‘lmagansiz.", show_alert=True)
        return
    await c.answer("✅ Obuna tasdiqlandi!")
    await c.message.edit_text("⛏️ <b>CraftHub</b> menyusi:", parse_mode="HTML", reply_markup=menu())

@router.callback_query(F.data == "ai")
async def ai_info(c: CallbackQuery):
    await c.message.answer("🤖 Minecraft savolingizni yozing. Masalan: \"1.21.x iron farm qanday?\"")

@router.callback_query(F.data == "servers")
async def servers(c: CallbackQuery):
    async with Session() as db:
        rows = (await db.execute(select(Server).where(Server.active == True).limit(20))).scalars().all()
    if not rows:
        return await c.message.answer("🎮 Hozircha serverlar qo‘shilmagan.")
    await c.message.answer("\n\n".join(
        f"🎮 <b>{escape(s.name)}</b>\n🟢 {escape(s.host)}:{s.port}\n"
        f"🧩 {escape(s.version or 'N/A')} · {escape(s.mode or 'N/A')}"
        for s in rows
    ), parse_mode="HTML")

@router.callback_query(F.data.in_({"maps","builds","achievements","commands"}))
async def placeholder(c: CallbackQuery):
    labels = {"maps":"🗺️ Maps","builds":"🧱 Builds","achievements":"🏆 Achievements","commands":"🧪 Command Lab"}
    await c.message.answer(f"{labels[c.data]}\n\n🚧 Bu modul keyingi buildda kengaytiriladi.")

@router.callback_query(F.data == "profile")
async def profile(c: CallbackQuery):
    async with Session() as db:
        u = (await db.execute(select(User).where(User.telegram_id == c.from_user.id))).scalar_one()
    await c.message.answer(
        f"👤 <b>Profil</b>\n\n🆔 <code>{u.telegram_id}</code>\n"
        f"👤 @{escape(u.username or 'none')}\n💎 Coins: {u.coins}\n"
        f"🤖 AI today: {u.ai_today}/{AI_DAILY_LIMIT}\n\n"
        f"🔗 https://t.me/{BOT_USERNAME}?start={u.referral}",
        parse_mode="HTML"
    )

@router.message(Command("menu"))
async def menu_cmd(m: Message):
    await m.answer("⛏️ CraftHub", reply_markup=menu())

@router.message(F.text)
async def chat(m: Message):
    if m.text.startswith("/"):
        return
    u = await get_user(m.from_user)
    if u.banned:
        return await m.answer("🚫 Siz bloklangansiz.")
    missing = await subscribed(m.from_user.id)
    if missing:
        return await m.answer("🔒 Avval majburiy kanallarga obuna bo‘ling.",
                               reply_markup=sub_keyboard(missing))
    await bot.send_chat_action(m.chat.id, "typing")
    await m.answer(await ask_ai(m.from_user.id, m.text))

serializer = URLSafeTimedSerializer(SECRET_KEY, salt="crafthub-admin")
def session_token():
    return serializer.dumps({"admin": True})
def is_admin(request):
    token = request.cookies.get("admin_session")
    if not token:
        raise HTTPException(401, "Login required")
    try:
        return serializer.loads(token, max_age=86400)
    except (BadSignature, SignatureExpired):
        raise HTTPException(401, "Session expired")

app = FastAPI(title="CraftHub")

@app.get("/", response_class=HTMLResponse)
async def home():
    return """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>CraftHub</title>
<style>body{margin:0;background:#090d0f;color:#fff;font-family:system-ui;text-align:center;padding:18vh 20px}
h1{font-size:60px;margin:10px}p{color:#9ba6ad;font-size:20px}a{display:inline-block;background:#70d889;color:#061008;padding:14px 22px;border-radius:12px;text-decoration:none;font-weight:800}</style></head>
<body><div>⛏️</div><h1>CraftHub</h1><p>Minecraft AI · Servers · Maps · Builds · Community</p><a href="https://t.me/crafthubot">Open @crafthubot</a></body></html>"""

@app.get("/health")
async def health():
    return {"status":"ok","service":"crafthub"}

@app.post("/telegram/webhook")
async def webhook(request: Request):
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(403, "Invalid secret")
    from aiogram.types import Update
    await dp.feed_update(bot, Update.model_validate(await request.json()))
    return {"ok": True}

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return ADMIN_HTML

@app.post("/admin/login")
async def admin_login(username: str = Form(...), password: str = Form(...)):
    if username != "admin" or password != ADMIN_PASSWORD:
        raise HTTPException(401, "Wrong credentials")
    r = JSONResponse({"ok":True})
    r.set_cookie("admin_session", session_token(), httponly=True, samesite="lax", secure=BASE_URL.startswith("https"))
    return r

@app.post("/admin/logout")
async def admin_logout():
    r=JSONResponse({"ok":True}); r.delete_cookie("admin_session"); return r

@app.get("/admin/stats")
async def stats(request: Request):
    is_admin(request)
    async with Session() as db:
        return {
            "users": await db.scalar(select(func.count()).select_from(User)) or 0,
            "channels": await db.scalar(select(func.count()).select_from(Channel)) or 0,
            "servers": await db.scalar(select(func.count()).select_from(Server)) or 0,
        }

@app.get("/admin/channels")
async def channels(request: Request):
    is_admin(request)
    async with Session() as db:
        rows=(await db.execute(select(Channel).order_by(Channel.id.desc()))).scalars().all()
    return [{"id":x.id,"chat_id":x.chat_id,"title":x.title,"join_url":x.join_url} for x in rows]

@app.post("/admin/channels")
async def add_channel(request: Request, chat_id:str=Form(...), title:str=Form(...), join_url:str=Form(...)):
    is_admin(request)
    async with Session() as db:
        db.add(Channel(chat_id=chat_id,title=title,join_url=join_url)); await db.commit()
    return {"ok":True}

@app.delete("/admin/channels/{cid}")
async def del_channel(request: Request,cid:int):
    is_admin(request)
    async with Session() as db:
        x=await db.get(Channel,cid)
        if not x: raise HTTPException(404,"Not found")
        await db.delete(x); await db.commit()
    return {"ok":True}

@app.get("/admin/users")
async def users(request: Request):
    is_admin(request)
    async with Session() as db:
        rows=(await db.execute(select(User).order_by(User.id.desc()).limit(200))).scalars().all()
    return [{"id":u.telegram_id,"username":u.username,"name":u.first_name,"banned":u.banned,"coins":u.coins} for u in rows]

@app.post("/admin/users/{uid}/ban")
async def ban(request: Request,uid:int):
    is_admin(request)
    async with Session() as db:
        u=(await db.execute(select(User).where(User.telegram_id==uid))).scalar_one_or_none()
        if not u: raise HTTPException(404,"Not found")
        u.banned=True; await db.commit()
    return {"ok":True}

ADMIN_HTML = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>CraftHub Admin</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#080c0e;color:#eaf0f2;font-family:system-ui}#login{min-height:100vh;display:grid;place-items:center}.card{background:#11181b;border:1px solid #253035;border-radius:16px;padding:22px;width:min(440px,92vw)}input,button{width:100%;padding:12px;margin:6px 0;border-radius:9px;border:1px solid #2b363b;background:#0a0f11;color:#fff}button{background:#70d889;color:#071008;border:0;font-weight:800;cursor:pointer}.hide{display:none}aside{width:230px;position:fixed;inset:0 auto 0 0;background:#0d1316;padding:18px}aside button{background:transparent;color:#b5c0c5;text-align:left}main{margin-left:230px;padding:30px}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:15px}.stat{background:#11181b;padding:20px;border-radius:15px}.stat b{font-size:32px;display:block}.row{padding:12px;border-bottom:1px solid #20292d}@media(max-width:700px){aside{position:static;width:100%}main{margin:0}.stats{grid-template-columns:1fr}}
</style></head><body>
<div id="login"><form class="card" id="lf"><h1>⛏️ CraftHub Admin</h1><input name="username" value="admin"><input name="password" type="password" placeholder="Password"><button>Login</button><p id="err"></p></form></div>
<div id="app" class="hide"><aside><h2>⛏️ CraftHub</h2><button onclick="page('dash')">📊 Dashboard</button><button onclick="page('users')">👥 Users</button><button onclick="page('channels')">📢 Channels</button><button onclick="logout()">Logout</button></aside>
<main><section id="dash"><h1>Dashboard</h1><div class="stats"><div class="stat">Users<b id="u">0</b></div><div class="stat">Channels<b id="c">0</b></div><div class="stat">Servers<b id="s">0</b></div></div></section>
<section id="users" class="hide"><h1>Users</h1><div id="ut"></div></section>
<section id="channels" class="hide"><h1>Force Subscribe</h1><form class="card" id="cf"><input name="chat_id" placeholder="-100..." required><input name="title" placeholder="Channel title" required><input name="join_url" placeholder="https://t.me/..." required><button>Add channel</button></form><div id="ct"></div></section>
</main></div>
<script>
const $=x=>document.querySelector(x); async function api(u,o={}){let r=await fetch(u,{...o,credentials:"same-origin"});if(!r.ok)throw Error((await r.json().catch(()=>({detail:"Error"}))).detail);return r.json()}
function page(x){["dash","users","channels"].forEach(y=>$("#"+y).classList.toggle("hide",y!==x));if(x==="dash")stats();if(x==="users")users();if(x==="channels")channels()}
async function stats(){let x=await api("/admin/stats");$("#u").textContent=x.users;$("#c").textContent=x.channels;$("#s").textContent=x.servers}
async function users(){let x=await api("/admin/users");$("#ut").innerHTML=x.map(u=>`<div class="row">👤 ${u.name||""} @${u.username||"none"} — ${u.id} — ${u.banned?"🚫":"🟢"} <button onclick="ban(${u.id})">Ban</button></div>`).join("")}
async function ban(id){await api("/admin/users/"+id+"/ban",{method:"POST"});users()}
async function channels(){let x=await api("/admin/channels");$("#ct").innerHTML=x.map(c=>`<div class="row">📢 ${c.title} — ${c.chat_id}</div>`).join("")}
$("#lf").onsubmit=async e=>{e.preventDefault();try{await api("/admin/login",{method:"POST",body:new FormData(e.target)});$("#login").classList.add("hide");$("#app").classList.remove("hide");stats()}catch(x){$("#err").textContent=x.message}}
$("#cf").onsubmit=async e=>{e.preventDefault();await api("/admin/channels",{method:"POST",body:new FormData(e.target)});e.target.reset();channels();stats()}
async function logout(){await api("/admin/logout",{method:"POST"});location.reload()}
</script></body></html>"""

async def run():
    await init_db()
    if RUN_MODE == "polling":
        await dp.start_polling(bot)

@app.on_event("startup")
async def startup():
    await init_db()
    if RUN_MODE == "webhook" and BASE_URL:
        await bot.set_webhook(BASE_URL.rstrip("/")+"/telegram/webhook", secret_token=WEBHOOK_SECRET or None)
    elif RUN_MODE == "polling":
        asyncio.create_task(dp.start_polling(bot))

@app.on_event("shutdown")
async def shutdown():
    try: await bot.session.close()
    except: pass

if __name__ == "__main__":
    asyncio.run(run())
