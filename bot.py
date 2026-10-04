import os, csv, io, sqlite3, logging
from datetime import datetime, timezone
from html import escape
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, MenuButtonCommands
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

load_dotenv()
BOT_TOKEN=os.getenv('BOT_TOKEN','').strip(); GROUP_ID=int(os.getenv('GROUP_ID','0')); REPORT_CHAT_ID=int(os.getenv('REPORT_CHAT_ID',str(GROUP_ID)))
DB_FILE=os.getenv('DB_FILE','/data/media_bank.db' if os.path.isdir('/data') else 'media_bank.db'); ENABLE_BUTTON_PREMIUM=os.getenv('ENABLE_BUTTON_PREMIUM','1')=='1'
DAILY_REPORT_HOUR=int(os.getenv('DAILY_REPORT_HOUR','21')); DAILY_REPORT_MINUTE=int(os.getenv('DAILY_REPORT_MINUTE','0'))
WEEKLY_REPORT_DAY=int(os.getenv('WEEKLY_REPORT_DAY','0')); WEEKLY_REPORT_HOUR=int(os.getenv('WEEKLY_REPORT_HOUR','21')); WEEKLY_REPORT_MINUTE=int(os.getenv('WEEKLY_REPORT_MINUTE','5'))
ADMIN_IDS={8431733595}; MANAGERS={8431733595:'Эдуард',8923153510:'Александр'}
BANKS={'debit':['Т-Банк','Альфа-Банк','ВТБ-Банк','Промсвязьбанк','Ак Барс Банк','ОТП банк'],'credit':['Т-Банк','ВТБ','Уралсиб','ОТП-Банк','Яндекс — Кредитная карта супер Сплит','Альфа-Банк'],'rko':['Альфа-Банк','Промсвязьбанк','РКО от Санкт-Петербург Банка','УБРиР Банк']}
PRODUCTS={'debit':'Дебетовая карта','credit':'Кредитная карта','rko':'Регистрация бизнеса + РКО'}
EMOJI_IDS='5438496463044752972,5445353829304387411,5287231198098117669,5278702045883292456,5197269100878907942,5416117059207572332,5210952531676504517,5206607081334906820,5373012449597335010,5190498849440931467,5447410659077661506,5373012449597335010,5382194935057372936,5210956306952758910,5197269100878907942,5332455502917949981,5445353829304387411,5287231198098117669,5278702045883292456,5253742260054409879,5382357040008021292,5386367538735104399,5206607081334906820,5444856076954520455,5193177581888755275,5379999674193172777,5206607081334906820,5210952531676504517,5190498849440931467,5231200819986047254,5197269100878907942,5244837092042750681,5190498849440931467,5386367538735104399,5206607081334906820,5253742260054409879,5217822164362739968,5341715473882955310,5231200819986047254,5244837092042750681,5197269100878907942,5382357040008021292,5386367538735104399,5206607081334906820,5190498849440931467,5445355530111437729,5443127283898405358,5231012545799666522,5231200819986047254,5244837092042750681,5246762912428603768,5303214794336125778,5274055917766202507,5413879192267805083,5382194935057372936,5382194935057372936,5287231198098117669,5310278924616356636,5440539497383087970,5424972470023104089,5244837092042750681,5231200819986047254,5413879192267805083,5274055917766202507,5274055917766202507,5444856076954520455,5382357040008021292,5386367538735104399,5206607081334906820,5190498849440931467,5445353829304387411,5287231198098117669,5278702045883292456,5332455502917949981,5458603043203327669,5424818078833715060,5395695537687123235,5461117441612462242,5456140674028019486,5424972470023104089,5341715473882955310,5197371802136892976,5447644880824181073,5445267414562389170,5395444784611480792,5206607081334906820,5197288647275071607,5251203410396458957,5197288647275071607,5271604874419647061'.split(',')
E={'welcome':0,'debit':1,'credit':2,'rko':3,'form':4,'review':6,'back':5,'send':7,'manager':9,'manager_select':9,'manager_admin':9,'user':8,'apps':4,'apps_all':4,'edit':4,'cancel':5,'new':20,'work':21,'done':22,'open':23,'search':25,'admin':36,'stats':38,'report':39,'export':65,'bank':15}
BOT_VERSION='MEDIA-BANK-PREMIUM-REVIEW-FIX-6.1'
# Requested Premium Emoji mapping: all-applications=5197269100878907942; back/cancel=5416117059207572332; managers=5190498849440931467; client name/Telegram=5373012449597335010.
# Premium UI: dedicated IDs for review, manager selection, all-applications, and admin managers.

logging.basicConfig(format='%(asctime)s | %(levelname)s | %(message)s',level=logging.INFO); log=logging.getLogger('media_bank')
def emoji(k,f='•'):
    if k not in E:
        return escape(f)
    return f'<tg-emoji emoji-id="{escape(EMOJI_IDS[E[k]])}">{escape(f)}</tg-emoji>'

# Review screen: use dedicated Premium Emoji IDs from the user's 90-ID set.
REVIEW_EMOJI = {
    'title': '5197269100878907942',
    'product_debit': '5445353829304387411',
    'product_credit': '5287231198098117669',
    'product_rko': '5278702045883292456',
    'bank': '5332455502917949981',
    'client': '5373012449597335010',
    'manager': '5190498849440931467',
}
def review_emoji(kind, fallback='•'):
    eid = REVIEW_EMOJI[kind]
    return f'<tg-emoji emoji-id=\"{eid}\">{escape(fallback)}</tg-emoji>'

def strip_custom_emoji(text):
    import re
    return re.sub(r'<tg-emoji\s+emoji-id="[^"]+">(.*?)</tg-emoji>', r'\1', text, flags=re.S)

def btn(text,k=None,data=None):
    overrides={'edit':'5197269100878907942','back':'5416117059207572332','cancel':'5416117059207572332','apps_all':'5197269100878907942','manager_admin':'5190498849440931467'}
    icon = overrides.get(k, EMOJI_IDS[E[k]] if k in E else None) if ENABLE_BUTTON_PREMIUM else None
    return InlineKeyboardButton(text, callback_data=data, icon_custom_emoji_id=icon)
def db(): return sqlite3.connect(DB_FILE)
def init_db():
    d=os.path.dirname(os.path.abspath(DB_FILE)); os.makedirs(d,exist_ok=True)
    with db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS applications(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,user_id INTEGER NOT NULL,username TEXT,product TEXT NOT NULL,bank TEXT NOT NULL,client_name TEXT NOT NULL,contact TEXT NOT NULL,manager TEXT NOT NULL,manager_id INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'new',taken_at TEXT,completed_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY,username TEXT,first_seen TEXT NOT NULL)''')
        # Миграция старых БД без потери заявок.
        app_cols={row[1] for row in c.execute('PRAGMA table_info(applications)').fetchall()}
        app_migrations={
            'user_id':'ALTER TABLE applications ADD COLUMN user_id INTEGER',
            'username':'ALTER TABLE applications ADD COLUMN username TEXT',
            'product':'ALTER TABLE applications ADD COLUMN product TEXT',
            'bank':'ALTER TABLE applications ADD COLUMN bank TEXT',
            'client_name':'ALTER TABLE applications ADD COLUMN client_name TEXT',
            'contact':'ALTER TABLE applications ADD COLUMN contact TEXT',
            'manager':'ALTER TABLE applications ADD COLUMN manager TEXT',
            'manager_id':'ALTER TABLE applications ADD COLUMN manager_id INTEGER',
            'status':"ALTER TABLE applications ADD COLUMN status TEXT DEFAULT 'new'",
            'taken_at':'ALTER TABLE applications ADD COLUMN taken_at TEXT',
            'completed_at':'ALTER TABLE applications ADD COLUMN completed_at TEXT',
        }
        for col,sql in app_migrations.items():
            if col not in app_cols:
                c.execute(sql)
        user_cols={row[1] for row in c.execute('PRAGMA table_info(users)').fetchall()}
        if 'first_seen' not in user_cols:
            c.execute("ALTER TABLE users ADD COLUMN first_seen TEXT")
            c.execute("UPDATE users SET first_seen=? WHERE first_seen IS NULL",(now(),))
        if 'username' not in user_cols:
            c.execute('ALTER TABLE users ADD COLUMN username TEXT')
        c.execute("UPDATE applications SET status='new' WHERE status IS NULL OR status=''")
        c.commit()
def now(): return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
def status(s): return {'new':emoji('new','🆕')+' <b>Новая</b>','in_work':emoji('work','🔄')+' <b>В работе</b>','completed':emoji('done','✅')+' <b>Завершена</b>'}.get(s,s)
def home_kb(uid):
    r=[[btn('Дебетовая карта','debit','product:debit')],[btn('Кредитная карта','credit','product:credit')],[btn('Регистрация бизнеса + РКО','rko','product:rko')],[btn('Оставить заявку','form','form')]]
    if uid in ADMIN_IDS:r.append([btn('Админ-панель','admin','admin')])
    if uid in MANAGERS:r.append([btn('Панель менеджера','manager','manager_panel')])
    return InlineKeyboardMarkup(r)
def bank_kb(p): return InlineKeyboardMarkup([[btn(b,'bank',f'bank:{p}:{i}')] for i,b in enumerate(BANKS[p])]+[[btn('Назад','back','home')]])
def manager_kb(): return InlineKeyboardMarkup([[btn('Эдуард','manager_select','mgr:8431733595')],[btn('Александр','manager_select','mgr:8923153510')],[btn('Назад','back','home')]])
def form_kb(): return InlineKeyboardMarkup([[btn('Назад','back','home')]])
def review_kb(): return InlineKeyboardMarkup([[btn('Отправить заявку','send','submit')],[btn('Изменить','edit','edit')],[btn('Отмена','cancel','cancel')]])
def actions(a,s):
    r=[]
    if s=='new':r.append([btn('Взять в работу','work',f'take:{a}')])
    elif s=='in_work':r.append([btn('Завершить','done',f'complete:{a}')])
    r.append([btn('Открыть','open',f'view:{a}')]);
    return InlineKeyboardMarkup(r)
def admin_kb(): return InlineKeyboardMarkup([[btn('Все заявки','apps_all','apps:all')],[btn('Новые','new','apps:new'),btn('В работе','work','apps:in_work'),btn('Завершённые','done','apps:completed')],[btn('Статистика','stats','stats'),btn('Отчёт','report','report')],[btn('Менеджеры','manager_admin','managers')],[btn('Экспорт CSV','export','export')],[btn('Главное меню','back','home')]])
def manager_panel():
    return InlineKeyboardMarkup([
        [btn('Все заявки','apps_all','mgrapps:all')],
        [btn('Новые','new','mgrapps:new'), btn('В работе','work','mgrapps:in_work')],
        [btn('Завершённые','done','mgrapps:completed')],
        [btn('Мои заявки','apps','myapps')],
        [btn('Моя статистика','stats','mystats')],
        [btn('Мой отчёт','report','myreport')],
        [btn('Мой профиль','manager_admin','myprofile')],
        [btn('Главное меню','back','home')]
    ])

def manager_profile(uid):
    name=MANAGERS.get(uid,'Менеджер')
    with db() as c:
        total=c.execute('SELECT COUNT(*) FROM applications WHERE manager_id=?',(uid,)).fetchone()[0]
        new=c.execute("SELECT COUNT(*) FROM applications WHERE manager_id=? AND status='new'",(uid,)).fetchone()[0]
        work=c.execute("SELECT COUNT(*) FROM applications WHERE manager_id=? AND status='in_work'",(uid,)).fetchone()[0]
        done=c.execute("SELECT COUNT(*) FROM applications WHERE manager_id=? AND status='completed'",(uid,)).fetchone()[0]
    t=(emoji('manager','👤')+f' <b>Профиль менеджера</b>\n\n'
       f'<b>Имя:</b> {escape(name)}\n'
       f'<b>Telegram ID:</b> <code>{uid}</code>\n\n'
       f'{emoji("apps_all","📋")} <b>Всего заявок:</b> {total}\n'
       f'{emoji("new","🆕")} <b>Новые:</b> {new}\n'
       f'{emoji("work","🔄")} <b>В работе:</b> {work}\n'
       f'{emoji("done","✅")} <b>Завершено:</b> {done}')
    return t

async def edit(q,t,k):
    try:
        await q.edit_message_text(t, parse_mode=ParseMode.HTML, reply_markup=k)
    except Exception as e:
        msg=str(e)
        if 'Message is not modified' in msg:
            return
        if 'Entity_text_invalid' in msg or "can't parse entities" in msg:
            await q.edit_message_text(strip_custom_emoji(t), parse_mode=ParseMode.HTML, reply_markup=k)
            return
        raise

async def safe_reply(message,text,**kwargs):
    try:
        return await message.reply_text(text,**kwargs)
    except Exception as e:
        msg=str(e)
        if 'Entity_text_invalid' in msg or "can't parse entities" in msg:
            kwargs.pop('parse_mode',None)
            return await message.reply_text(strip_custom_emoji(text),**kwargs)
        raise

async def safe_send(bot,chat_id,text,**kwargs):
    try:
        return await bot.send_message(chat_id,text,**kwargs)
    except Exception as e:
        msg=str(e)
        if 'Entity_text_invalid' in msg or "can't parse entities" in msg:
            kwargs.pop('parse_mode',None)
            return await bot.send_message(chat_id,strip_custom_emoji(text),**kwargs)
        raise
async def start(update,context):
    u=update.effective_user; context.user_data.clear()
    with db() as c:c.execute('INSERT OR REPLACE INTO users(user_id,username,first_seen) VALUES(?,?,COALESCE((SELECT first_seen FROM users WHERE user_id=?),?))',(u.id,u.username or '',u.id,now()));c.commit()
    t=emoji('welcome','⭐')+' <b>Media Bank</b>\n\nВыберите интересующую вас услугу:'
    if update.callback_query: await edit(update.callback_query,t,home_kb(u.id))
    else: await safe_reply(update.message,t,parse_mode=ParseMode.HTML,reply_markup=home_kb(u.id))
async def stats_text(mid=None):
    extra=' AND manager_id=?' if mid else ''; a=(mid,) if mid else ()
    with db() as c:
        q=lambda x:c.execute('SELECT COUNT(*) FROM applications WHERE '+x+extra,a).fetchone()[0]
        total=q('1=1'); new=q("status='new'"); work=q("status='in_work'"); done=q("status='completed'"); today=q("substr(created_at,1,10)=date('now')")
    title=emoji('stats','📊')+' <b>Статистика</b>'+((f' · {escape(MANAGERS[mid])}') if mid else '')
    return f'{title}\n\n📋 Всего: <b>{total}</b>\n{emoji("new","🆕")} Новых: <b>{new}</b>\n{emoji("work","🔄")} В работе: <b>{work}</b>\n{emoji("done","✅")} Завершено: <b>{done}</b>\nСегодня: <b>{today}</b>'
async def submit(update,context):
    d=context.user_data;u=update.effective_user; created=now()
    required=('product','bank','client_name','contact','manager','manager_id')
    missing=[k for k in required if not d.get(k)]
    if missing:
        await safe_reply(update.effective_message,'⚠️ <b>Не удалось отправить заявку.</b>\n\nФорма заполнена не полностью. Вернитесь назад и заполните данные ещё раз.',parse_mode=ParseMode.HTML,reply_markup=home_kb(u.id))
        log.error('SUBMIT VALIDATION ERROR: missing=%s data_keys=%s',missing,list(d.keys()))
        return
    with db() as c:
        # Совместимость со старой БД: в ней могло остаться обязательное поле `name`.
        cols={row[1] for row in c.execute('PRAGMA table_info(applications)').fetchall()}
        if 'name' in cols:
            cur=c.execute('INSERT INTO applications(created_at,user_id,username,product,bank,client_name,contact,manager,manager_id,status,name) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (created,u.id,u.username or '',d['product'],d['bank'],d['client_name'],d['contact'],d['manager'],d['manager_id'],'new',d['client_name']))
        else:
            cur=c.execute('INSERT INTO applications(created_at,user_id,username,product,bank,client_name,contact,manager,manager_id,status) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (created,u.id,u.username or '',d['product'],d['bank'],d['client_name'],d['contact'],d['manager'],d['manager_id'],'new'))
        a=cur.lastrowid;c.commit()
    t=emoji('new','🆕')+f' <b>Новая заявка #{a}</b>\n\n'+f'{emoji("form","📝")} <b>Продукт:</b> {escape(PRODUCTS[d["product"]])}\n'+f'{emoji("bank","🏦")} <b>Банк:</b> {escape(d["bank"])}\n'+f'{emoji("user","👤")} <b>Имя:</b> {escape(d["client_name"])}\n'+f'{emoji("user","👤")} <b>Telegram:</b> {escape(d["contact"])}\n'+f'{emoji("manager","🤝")} <b>Менеджер:</b> {escape(d["manager"])}\n🆕 <b>Статус:</b> Новая\n<i>{escape(created)}</i>'
    group_sent=True
    try:
        await safe_send(context.bot,GROUP_ID,t,parse_mode=ParseMode.HTML,reply_markup=actions(a,'new'))
    except Exception as ex:
        group_sent=False
        log.exception('GROUP SEND ERROR for application #%s: %s',a,ex)
    context.user_data.clear()
    if group_sent:
        msg=emoji('done','✅')+' <b>Заявка отправлена!</b>\n\nНомер заявки: <b>#'+str(a)+'</b>\nМенеджер свяжется с вами.'
    else:
        msg='⚠️ <b>Заявка #'+str(a)+' сохранена</b>\n\nНе удалось отправить уведомление в рабочую группу. Заявка не потеряна и доступна в панелях менеджера/администратора.'
    await safe_reply(update.effective_message,msg,parse_mode=ParseMode.HTML,reply_markup=home_kb(u.id))

async def text_input(update,context):
    s=context.user_data.get('step');v=(update.message.text or '').strip()
    if not s or not v:return
    if s=='name': context.user_data['client_name']=v;context.user_data['step']='contact';await safe_reply(update.message,emoji('user','👤')+' <b>Введите Telegram клиента:</b>\nНапример: @username',parse_mode=ParseMode.HTML,reply_markup=form_kb())
    elif s=='contact':context.user_data['contact']=v;context.user_data['step']='manager';await safe_reply(update.message,emoji('manager','🤝')+' <b>Выберите менеджера:</b>',parse_mode=ParseMode.HTML,reply_markup=manager_kb())
async def view(q,a):
    with db() as c:r=c.execute('SELECT id,created_at,product,bank,client_name,contact,manager,status FROM applications WHERE id=?',(a,)).fetchone()
    if not r:
        await edit(q,'⚠️ <b>Заявка не найдена.</b>',manager_panel() if q.from_user.id in MANAGERS else admin_kb())
        return
    t=f'{emoji("open","📂")} <b>Заявка #{r[0]}</b>\n\n{emoji("form","📝")} <b>Продукт:</b> {escape(PRODUCTS[r[2]])}\n{emoji("bank","🏦")} <b>Банк:</b> {escape(r[3])}\n{emoji("user","👤")} <b>Имя:</b> {escape(r[4])}\n{emoji("user","👤")} <b>Telegram:</b> {escape(r[5])}\n{emoji("manager","🤝")} <b>Менеджер:</b> {escape(r[6])}\n{status(r[7])}\n\n<b>Создана:</b> {escape(r[1])}'
    await edit(q,t,actions(r[0],r[7]))
async def apps(update,context,status_filter='all',mid=None,force_all=False):
    uid=update.effective_user.id
    is_admin=uid in ADMIN_IDS
    if mid is None and not is_admin and not force_all: mid=uid
    cnd=[];args=[]
    if status_filter!='all': cnd.append('status=?');args.append(status_filter)
    if mid is not None: cnd.append('manager_id=?');args.append(mid)
    w=(' WHERE '+' AND '.join(cnd)) if cnd else ''
    with db() as c:
        rows=c.execute(f'SELECT id,product,bank,client_name,manager,status FROM applications{w} ORDER BY id DESC LIMIT 30',args).fetchall()
    if is_admin:
        title='Все заявки' if mid is None else f'Заявки менеджера {escape(MANAGERS.get(mid,""))}'
        back='admin'
    else:
        title='Все заявки' if force_all else 'Мои заявки'
        back='manager_panel'
    buttons=[]
    for r in rows:
        buttons.append([btn(f'Открыть #{r[0]}','open',f'view:{r[0]}')])
    if not is_admin:
        if force_all:
            buttons.extend([
                [btn('Все заявки','apps_all','mgrapps:all')],
                [btn('Новые','new','mgrapps:new'), btn('В работе','work','mgrapps:in_work')],
                [btn('Завершённые','done','mgrapps:completed')],
            ])
        else:
            buttons.extend([
                [btn('Все мои','apps_all','myapps'), btn('Новые','new','myapps:new')],
                [btn('В работе','work','myapps:in_work'), btn('Завершённые','done','myapps:completed')],
            ])
    buttons.append([btn('Назад','back',back)])
    t=emoji('apps_all','📋')+f' <b>{title}</b>\n\n'+('\n\n'.join(f'<b>#{r[0]}</b> · {escape(r[3])}\n{escape(PRODUCTS[r[1]])} · {escape(r[2])}\n{escape(r[4])} · {escape(r[5])}' for r in rows) if rows else 'Заявок пока нет.')
    await edit(update.callback_query,t,InlineKeyboardMarkup(buttons))
async def report(bot,mid=None):
    q='SELECT manager,COUNT(*),SUM(status="completed") FROM applications WHERE created_at>=datetime("now","-7 day")'+(' AND manager_id=?' if mid else '')+' GROUP BY manager';args=(mid,) if mid else ()
    with db() as c:r=c.execute(q,args).fetchall()
    t=emoji('report','📈')+' <b>Отчёт Media Bank за 7 дней</b>\n\n'+(''.join(f'• {escape(x[0])}: {x[1]} заявок, {x[2] or 0} завершено\n' for x in r) if r else 'Заявок за период нет.')
    await safe_send(bot,REPORT_CHAT_ID,t,parse_mode=ParseMode.HTML)
async def export_csv(update,context):
    if update.effective_user.id not in ADMIN_IDS:return
    with db() as c:r=c.execute('SELECT id,created_at,product,bank,client_name,contact,manager,status FROM applications ORDER BY id DESC').fetchall()
    s=io.StringIO();w=csv.writer(s);w.writerow(['ID','Created','Product','Bank','Client','Telegram','Manager','Status']);w.writerows(r);f=io.BytesIO(s.getvalue().encode('utf-8-sig'));f.name='media_bank_applications.csv';await update.effective_message.reply_document(f,caption='Экспорт заявок Media Bank')
async def callback(update,context):
    q=update.callback_query
    u=q.from_user
    d=q.data or ''
    try:
        # Always acknowledge the callback first so Telegram does not leave
        # the button in a loading state.
        await q.answer()

        if d=='home':
            return await start(update,context)

        if d=='form':
            context.user_data.clear()
            return await edit(q,emoji('form','📝')+' <b>Выберите продукт:</b>',
                InlineKeyboardMarkup([
                    [btn('Дебетовая карта','debit','product:debit')],
                    [btn('Кредитная карта','credit','product:credit')],
                    [btn('Регистрация бизнеса + РКО','rko','product:rko')],
                    [btn('Назад','back','home')]
                ]))

        if d.startswith('product:'):
            p=d.split(':',1)[1]
            if p not in PRODUCTS:
                return await edit(q,'⚠️ <b>Неизвестный продукт.</b>',home_kb(u.id))
            context.user_data['product']=p
            return await edit(q,emoji(p,'•')+f' <b>{PRODUCTS[p]}</b>\n\nВыберите банк:',bank_kb(p))

        if d.startswith('banks:'):
            p=d.split(':',1)[1]
            if p not in PRODUCTS:
                return await edit(q,'⚠️ <b>Неизвестный продукт.</b>',home_kb(u.id))
            context.user_data['product']=p
            return await edit(q,emoji('bank','🏦')+' <b>Выберите банк:</b>',bank_kb(p))

        if d.startswith('bank:'):
            parts=d.split(':')
            if len(parts)!=3:
                return await edit(q,'⚠️ <b>Некорректный выбор банка.</b>',home_kb(u.id))
            _,p,i=parts
            try:
                bank=BANKS[p][int(i)]
            except (KeyError,ValueError,IndexError):
                return await edit(q,'⚠️ <b>Банк не найден.</b>',home_kb(u.id))
            context.user_data.update(product=p,bank=bank,step='name')
            return await edit(q,emoji('user','👤')+' <b>Введите имя клиента:</b>',form_kb())

        if d=='edit':
            context.user_data['step']='name'
            return await edit(q,emoji('edit','✏️')+' <b>Введите имя клиента заново:</b>',form_kb())

        if d=='cancel':
            context.user_data.clear()
            return await start(update,context)

        if d.startswith('mgr:'):
            try:
                m=int(d.split(':',1)[1])
                manager_name=MANAGERS[m]
            except (ValueError,KeyError,IndexError):
                return await edit(q,'⚠️ <b>Менеджер не найден.</b>',home_kb(u.id))
            x=context.user_data
            required=('product','bank','client_name','contact')
            if any(k not in x for k in required):
                context.user_data.clear()
                return await edit(q,'⚠️ <b>Сессия заявки устарела. Начните заявку заново.</b>',home_kb(u.id))
            context.user_data.update(manager_id=m,manager=manager_name,step='review')
            product_key=x["product"]
            product_emoji={'debit':'product_debit','credit':'product_credit','rko':'product_rko'}[product_key]
            fallback={'debit':'💳','credit':'💰','rko':'🏢'}[product_key]
            t=(
                review_emoji('title','📝')+' <b>Проверьте заявку</b>\n\n'
                f'{review_emoji(product_emoji,fallback)} <b>Продукт:</b> {escape(PRODUCTS[product_key])}\n'
                f'{review_emoji("bank","🏦")} <b>Банк:</b> {escape(x["bank"])}\n'
                f'{review_emoji("client","👤")} <b>Имя:</b> {escape(x["client_name"])}\n'
                f'{review_emoji("client","👤")} <b>Telegram:</b> {escape(x["contact"])}\n'
                f'{review_emoji("manager","🤝")} <b>Менеджер:</b> {escape(x["manager"])}'
            )
            return await edit(q,t,review_kb())

        if d=='submit':
            required=('product','bank','client_name','contact','manager','manager_id')
            if any(k not in context.user_data for k in required):
                return await edit(q,'⚠️ <b>Данные заявки заполнены не полностью.</b>\n\nНачните заявку заново.',home_kb(u.id))
            return await submit(update,context)

        if d=='admin':
            if u.id not in ADMIN_IDS:
                return
            return await edit(q,emoji('admin','⚙️')+' <b>Админ-панель</b>',admin_kb())

        if d=='manager_panel':
            if u.id not in MANAGERS:
                return
            return await edit(q,emoji('manager','🤝')+' <b>Панель менеджера</b>',manager_panel())

        if d.startswith('apps:'):
            if u.id not in ADMIN_IDS:
                return
            return await apps(update,context,d.split(':',1)[1])

        if d.startswith('mgrapps:'):
            if u.id not in MANAGERS:
                return
            return await apps(update,context,d.split(':',1)[1],None,True)

        if d=='myapps':
            if u.id not in MANAGERS:
                return
            return await apps(update,context,'all',u.id)

        if d.startswith('myapps:'):
            if u.id not in MANAGERS:
                return
            return await apps(update,context,d.split(':',1)[1],u.id)

        if d=='stats' and u.id in ADMIN_IDS:
            return await edit(q,await stats_text(),admin_kb())

        if d=='mystats' and u.id in MANAGERS:
            return await edit(q,await stats_text(u.id),manager_panel())

        if d=='report' and u.id in ADMIN_IDS:
            await report(context.bot)
            return await edit(q,emoji('done','✅')+' <b>Отчёт отправлен в рабочую группу.</b>',admin_kb())

        if d=='myreport' and u.id in MANAGERS:
            await report(context.bot,u.id)
            return await edit(q,emoji('done','✅')+' <b>Ваш отчёт отправлен в рабочую группу.</b>',manager_panel())

        if d=='myprofile' and u.id in MANAGERS:
            return await edit(q,manager_profile(u.id),
                InlineKeyboardMarkup([[btn('Назад','back','manager_panel')]]))

        if d=='export':
            if u.id not in ADMIN_IDS:
                return
            return await export_csv(update,context)

        if d=='managers' and u.id in ADMIN_IDS:
            return await edit(q,emoji('manager_admin','🤝')+' <b>Менеджеры</b>',
                InlineKeyboardMarkup([
                    [btn(n,'manager_select',f'mstats:{m}')]
                    for m,n in MANAGERS.items()
                ]+[[btn('Назад','back','admin')]]))

        if d.startswith('mstats:'):
            if u.id not in ADMIN_IDS:
                return
            try:
                mid=int(d.split(':',1)[1])
            except ValueError:
                return await edit(q,'⚠️ <b>Некорректный менеджер.</b>',admin_kb())
            if mid not in MANAGERS:
                return await edit(q,'⚠️ <b>Менеджер не найден.</b>',admin_kb())
            return await edit(q,await stats_text(mid),
                InlineKeyboardMarkup([[btn('Назад','back','managers')]]))

        if d.startswith('view:'):
            try:
                aid=int(d.split(':',1)[1])
            except ValueError:
                return await edit(q,'⚠️ <b>Некорректный номер заявки.</b>',home_kb(u.id))
            return await view(q,aid)

        if d.startswith('take:') and u.id in MANAGERS:
            try:
                a=int(d.split(':',1)[1])
            except ValueError:
                return await edit(q,'⚠️ <b>Некорректный номер заявки.</b>',manager_panel())
            with db() as c:
                cur=c.execute(
                    "UPDATE applications SET status='in_work',taken_at=? "
                    "WHERE id=? AND manager_id=? AND status='new'",
                    (now(),a,u.id)
                )
                c.commit()
            if cur.rowcount==0:
                return await edit(q,'⚠️ <b>Заявка не назначена вам или уже взята в работу.</b>',manager_panel())
            return await view(q,a)

        if d.startswith('complete:') and u.id in MANAGERS:
            try:
                a=int(d.split(':',1)[1])
            except ValueError:
                return await edit(q,'⚠️ <b>Некорректный номер заявки.</b>',manager_panel())
            with db() as c:
                cur=c.execute(
                    "UPDATE applications SET status='completed',completed_at=? "
                    "WHERE id=? AND manager_id=? AND status='in_work'",
                    (now(),a,u.id)
                )
                c.commit()
            if cur.rowcount==0:
                return await edit(q,'⚠️ <b>Заявка не находится в вашей работе.</b>',manager_panel())
            return await view(q,a)

    except Exception:
        log.exception("CALLBACK ERROR data=%r user=%s", d, getattr(u,'id',None))
        try:
            await safe_reply(
                update.effective_message,
                '⚠️ <b>Произошла ошибка.</b>\n\nПопробуйте ещё раз или вернитесь в главное меню.',
                parse_mode=ParseMode.HTML,
                reply_markup=home_kb(u.id)
            )
        except Exception:
            log.exception("Failed to send callback error message")

async def stats_cmd(update,context):
    u=update.effective_user
    if u.id in ADMIN_IDS:await safe_reply(update.message,await stats_text(),parse_mode=ParseMode.HTML,reply_markup=admin_kb())
    elif u.id in MANAGERS:await safe_reply(update.message,await stats_text(u.id),parse_mode=ParseMode.HTML,reply_markup=manager_panel())
async def myid_cmd(update,context):
    await update.message.reply_text(f"Ваш Telegram ID: <code>{update.effective_user.id}</code>", parse_mode=ParseMode.HTML)

async def report_cmd(update,context):
    if update.effective_user.id in ADMIN_IDS:await report(context.bot);await update.message.reply_text('Отчёт отправлен в рабочую группу.')
def backup_database():
    """Create a consistent SQLite backup in a sibling backups folder."""
    if not os.path.exists(DB_FILE):
        return None
    backup_dir=os.path.join(os.path.dirname(os.path.abspath(DB_FILE)), 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_UTC')
    target=os.path.join(backup_dir, f'media_bank_{stamp}.db')
    with sqlite3.connect(DB_FILE) as source, sqlite3.connect(target) as dest:
        source.backup(dest)
    # Keep the 14 most recent backups to limit disk usage.
    backups=sorted((os.path.join(backup_dir,n) for n in os.listdir(backup_dir) if n.endswith('.db')), key=os.path.getmtime, reverse=True)
    for old in backups[14:]:
        try: os.remove(old)
        except OSError: log.warning('Could not remove old backup: %s', old)
    log.info('Database backup created: %s', target)
    return target

async def scheduled_backup(context):
    try:
        backup_database()
    except Exception:
        log.exception('Scheduled database backup failed')

async def scheduled(context):
    d=datetime.now(timezone.utc)
    if d.hour==DAILY_REPORT_HOUR and d.minute==DAILY_REPORT_MINUTE:await report(context.bot)
    if d.weekday()==WEEKLY_REPORT_DAY and d.hour==WEEKLY_REPORT_HOUR and d.minute==WEEKLY_REPORT_MINUTE:await report(context.bot)
async def setup(app):
    # Меню команд Telegram
    commands = [
        BotCommand('start', '🏠 Главное меню'),
        BotCommand('stats', '📊 Статистика'),
        BotCommand('report', '📈 Отчёт'),
    ]
    await app.bot.set_my_commands(commands)
    await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
def main():
    if not BOT_TOKEN:raise RuntimeError('BOT_TOKEN is missing in .env')
    if not GROUP_ID:raise RuntimeError('GROUP_ID is missing in .env')
    init_db();app=Application.builder().token(BOT_TOKEN).post_init(setup).build();app.add_handler(CommandHandler('start',start));app.add_handler(CommandHandler('stats',stats_cmd));app.add_handler(CommandHandler('report',report_cmd));app.add_handler(CommandHandler('myid',myid_cmd));app.add_handler(CallbackQueryHandler(callback));app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_input));
    if app.job_queue:
        app.job_queue.run_repeating(scheduled,interval=60,first=10)
        app.job_queue.run_daily(scheduled_backup,time=__import__('datetime').time(hour=3,minute=15,tzinfo=timezone.utc),name='database_backup')
    print('MEDIA BANK — CLEAN FROM ZERO / FIXED HTML + PREMIUM');print('DB:',DB_FILE);print('Daily SQLite backups: 14 retained');print('Premium message emoji: enabled');print('Premium button icons:',ENABLE_BUTTON_PREMIUM);app.run_polling(drop_pending_updates=True)
if __name__=='__main__':main()
