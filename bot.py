import os, csv, io, sqlite3, logging
from datetime import datetime, timezone
from html import escape
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, MenuButtonCommands
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

load_dotenv()
BOT_TOKEN=os.getenv('BOT_TOKEN','').strip(); GROUP_ID=int(os.getenv('GROUP_ID','0')); REPORT_CHAT_ID=int(os.getenv('REPORT_CHAT_ID',str(GROUP_ID)))
DB_FILE=os.getenv('DB_FILE','media_bank.db'); ENABLE_BUTTON_PREMIUM=os.getenv('ENABLE_BUTTON_PREMIUM','1')=='1'
DAILY_REPORT_HOUR=int(os.getenv('DAILY_REPORT_HOUR','21')); DAILY_REPORT_MINUTE=int(os.getenv('DAILY_REPORT_MINUTE','0'))
WEEKLY_REPORT_DAY=int(os.getenv('WEEKLY_REPORT_DAY','0')); WEEKLY_REPORT_HOUR=int(os.getenv('WEEKLY_REPORT_HOUR','21')); WEEKLY_REPORT_MINUTE=int(os.getenv('WEEKLY_REPORT_MINUTE','5'))
ADMIN_IDS={6045840701}; MANAGERS={6045840701:'Эдуард',8923153510:'Александр'}
BANKS={'debit':['Т-Банк','Альфа-Банк','ВТБ-Банк','Промсвязьбанк','Ак Барс Банк','ОТП банк'],'credit':['Т-Банк','ВТБ','Уралсиб','ОТП-Банк','Яндекс — Кредитная карта супер Сплит','Альфа-Банк'],'rko':['Альфа-Банк','Промсвязьбанк','РКО от Санкт-Петербург Банка','УБРиР Банк']}
PRODUCTS={'debit':'Дебетовая карта','credit':'Кредитная карта','rko':'Регистрация бизнеса + РКО'}
EMOJI_IDS='5438496463044752972,5445353829304387411,5287231198098117669,5278702045883292456,5197269100878907942,5416117059207572332,5210952531676504517,5206607081334906820,5373012449597335010,5190498849440931467,5447410659077661506,5373012449597335010,5382194935057372936,5210956306952758910,5197269100878907942,5332455502917949981,5445353829304387411,5287231198098117669,5278702045883292456,5253742260054409879,5382357040008021292,5386367538735104399,5206607081334906820,5444856076954520455,5193177581888755275,5379999674193172777,5206607081334906820,5210952531676504517,5190498849440931467,5231200819986047254,5197269100878907942,5244837092042750681,5190498849440931467,5386367538735104399,5206607081334906820,5253742260054409879,5217822164362739968,5341715473882955310,5231200819986047254,5244837092042750681,5197269100878907942,5382357040008021292,5386367538735104399,5206607081334906820,5190498849440931467,5445355530111437729,5443127283898405358,5231012545799666522,5231200819986047254,5244837092042750681,5246762912428603768,5303214794336125778,5274055917766202507,5413879192267805083,5382194935057372936,5382194935057372936,5287231198098117669,5310278924616356636,5440539497383087970,5424972470023104089,5244837092042750681,5231200819986047254,5413879192267805083,5274055917766202507,5274055917766202507,5444856076954520455,5382357040008021292,5386367538735104399,5206607081334906820,5190498849440931467,5445353829304387411,5287231198098117669,5278702045883292456,5332455502917949981,5458603043203327669,5424818078833715060,5395695537687123235,5461117441612462242,5456140674028019486,5424972470023104089,5341715473882955310,5197371802136892976,5447644880824181073,5445267414562389170,5395444784611480792,5206607081334906820,5197288647275071607,5251203410396458957,5197288647275071607,5271604874419647061'.split(',')
E={'welcome':0,'debit':1,'credit':2,'rko':3,'form':4,'back':6,'send':7,'manager':8,'user':9,'apps':10,'edit':12,'cancel':19,'new':20,'work':21,'done':22,'open':23,'search':25,'admin':36,'stats':38,'report':39,'export':65,'bank':15}
logging.basicConfig(format='%(asctime)s | %(levelname)s | %(message)s',level=logging.INFO); log=logging.getLogger('media_bank')
def emoji(k,f='•'): return f'<tg-emoji emoji-id="{EMOJI_IDS[E[k]]}">{escape(f)}</tg-emoji>' if k in E else escape(f)
def btn(text,k=None,data=None): return InlineKeyboardButton(text,callback_data=data,icon_custom_emoji_id=EMOJI_IDS[E[k]] if ENABLE_BUTTON_PREMIUM and k in E else None)
def db(): return sqlite3.connect(DB_FILE)
def init_db():
    d=os.path.dirname(os.path.abspath(DB_FILE)); os.makedirs(d,exist_ok=True)
    with db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS applications(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,user_id INTEGER NOT NULL,username TEXT,product TEXT NOT NULL,bank TEXT NOT NULL,client_name TEXT NOT NULL,contact TEXT NOT NULL,manager TEXT NOT NULL,manager_id INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'new',taken_at TEXT,completed_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY,username TEXT,first_seen TEXT NOT NULL)''')

        # Безопасная миграция старой базы:
        # если таблица users уже существовала без first_seen,
        # добавляем колонку, не удаляя существующие данные.
        cols = {row[1] for row in c.execute("PRAGMA table_info(users)").fetchall()}
        if "first_seen" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN first_seen TEXT")
            c.execute("UPDATE users SET first_seen=datetime('now') WHERE first_seen IS NULL")

        c.commit()
def now(): return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
def status(s): return {'new':emoji('new','🆕')+' <b>Новая</b>','in_work':emoji('work','🔄')+' <b>В работе</b>','completed':emoji('done','✅')+' <b>Завершена</b>'}.get(s,s)
def home_kb(uid):
    r=[[btn('Дебетовая карта','debit','product:debit')],[btn('Кредитная карта','credit','product:credit')],[btn('Регистрация бизнеса + РКО','rko','product:rko')],[btn('Оставить заявку','form','form')]]
    if uid in ADMIN_IDS:r.append([btn('Админ-панель','admin','admin')])
    elif uid in MANAGERS:r.append([btn('Панель менеджера','manager','manager_panel')])
    return InlineKeyboardMarkup(r)
def bank_kb(p): return InlineKeyboardMarkup([[btn(b,'bank',f'bank:{p}:{i}')] for i,b in enumerate(BANKS[p])]+[[btn('Назад','back','home')]])
def manager_kb(): return InlineKeyboardMarkup([[btn('Эдуард','manager','mgr:6045840701')],[btn('Александр','manager','mgr:8923153510')],[btn('Назад','back','home')]])
def form_kb(): return InlineKeyboardMarkup([[btn('Назад','back','home')]])
def review_kb(): return InlineKeyboardMarkup([[btn('Отправить заявку','send','submit')],[btn('Изменить','edit','edit')],[btn('Отмена','cancel','cancel')]])
def actions(a,s):
    r=[]
    if s=='new':r.append([btn('Взять в работу','work',f'take:{a}')])
    elif s=='in_work':r.append([btn('Завершить','done',f'complete:{a}')])
    r.append([btn('Открыть','open',f'view:{a}')]); return InlineKeyboardMarkup(r)
def admin_kb(): return InlineKeyboardMarkup([[btn('Все заявки','apps','apps:all')],[btn('Новые','new','apps:new'),btn('В работе','work','apps:in_work'),btn('Завершённые','done','apps:completed')],[btn('Статистика','stats','stats'),btn('Отчёт','report','report')],[btn('Менеджеры','manager','managers')],[btn('Экспорт CSV','export','export')],[btn('Главное меню','back','home')]])
def manager_panel(): return InlineKeyboardMarkup([[btn('Мои заявки','apps','myapps')],[btn('Моя статистика','stats','mystats')],[btn('Мой отчёт','report','myreport')],[btn('Главное меню','back','home')]])
async def edit(q,t,k):
    try: await q.edit_message_text(t,parse_mode=ParseMode.HTML,reply_markup=k)
    except Exception as e:
        if 'Message is not modified' not in str(e): raise
async def start(update,context):
    u=update.effective_user; context.user_data.clear()
    with db() as c:c.execute('INSERT OR REPLACE INTO users(user_id,username,first_seen) VALUES(?,?,COALESCE((SELECT first_seen FROM users WHERE user_id=?),?))',(u.id,u.username or '',u.id,now()));c.commit()
    t=emoji('welcome','⭐')+' <b>Media Bank</b>\n\nВыберите интересующую вас услугу:'
    if update.callback_query: await edit(update.callback_query,t,home_kb(u.id))
    else: await update.message.reply_text(t,parse_mode=ParseMode.HTML,reply_markup=home_kb(u.id))
async def stats_text(mid=None):
    extra=' AND manager_id=?' if mid else ''; a=(mid,) if mid else ()
    with db() as c:
        q=lambda x:c.execute('SELECT COUNT(*) FROM applications WHERE '+x+extra,a).fetchone()[0]
        total=q('1=1'); new=q("status='new'"); work=q("status='in_work'"); done=q("status='completed'"); today=q("date(created_at)=date('now')")
    title=emoji('stats','📊')+' <b>Статистика</b>'+((f' · {escape(MANAGERS[mid])}') if mid else '')
    return f'{title}\n\n📋 Всего: <b>{total}</b>\n{emoji("new","🆕")} Новых: <b>{new}</b>\n{emoji("work","🔄")} В работе: <b>{work}</b>\n{emoji("done","✅")} Завершено: <b>{done}</b>\nСегодня: <b>{today}</b>'
async def submit(update,context):
    d=context.user_data;u=update.effective_user; created=now()
    with db() as c:
        cur=c.execute('INSERT INTO applications(created_at,user_id,username,product,bank,client_name,contact,manager,manager_id,status) VALUES(?,?,?,?,?,?,?,?,?,?)',(created,u.id,u.username or '',d['product'],d['bank'],d['client_name'],d['contact'],d['manager'],d['manager_id'],'new'));a=cur.lastrowid;c.commit()
    t=emoji('new','🆕')+f' <b>Новая заявка #{a}</b>\n\n'+f'{emoji("form","📝")} <b>Продукт:</b> {escape(PRODUCTS[d["product"]])}\n'+f'{emoji("bank","🏦")} <b>Банк:</b> {escape(d["bank"])}\n'+f'{emoji("user","👤")} <b>Имя:</b> {escape(d["client_name"])}\n'+f'{emoji("user","👤")} <b>Telegram:</b> {escape(d["contact"])}\n'+f'{emoji("manager","🤝")} <b>Менеджер:</b> {escape(d["manager"])}\n🆕 <b>Статус:</b> Новая\n<i>{escape(created)}</i>'
    await context.bot.send_message(GROUP_ID,t,parse_mode=ParseMode.HTML,reply_markup=actions(a,'new'));context.user_data.clear()
    await update.effective_message.reply_text(emoji('done','✅')+' <b>Заявка отправлена!</b>\n\nНомер заявки: <b>#'+str(a)+'</b>\nМенеджер свяжется с вами.',parse_mode=ParseMode.HTML,reply_markup=home_kb(u.id))
async def text_input(update,context):
    s=context.user_data.get('step');v=(update.message.text or '').strip()
    if not s or not v:return
    if s=='name': context.user_data['client_name']=v;context.user_data['step']='contact';await update.message.reply_text(emoji('user','👤')+' <b>Введите Telegram клиента:</b>\nНапример: @username',parse_mode=ParseMode.HTML,reply_markup=form_kb())
    elif s=='contact':context.user_data['contact']=v;context.user_data['step']='manager';await update.message.reply_text(emoji('manager','🤝')+' <b>Выберите менеджера:</b>',parse_mode=ParseMode.HTML,reply_markup=manager_kb())
async def view(q,a):
    with db() as c:r=c.execute('SELECT id,created_at,product,bank,client_name,contact,manager,status FROM applications WHERE id=?',(a,)).fetchone()
    if not r:await q.answer('Заявка не найдена',show_alert=True);return
    t=f'{emoji("open","📂")} <b>Заявка #{r[0]}</b>\n\n{emoji("form","📝")} <b>Продукт:</b> {escape(PRODUCTS[r[2]])}\n{emoji("bank","🏦")} <b>Банк:</b> {escape(r[3])}\n{emoji("user","👤")} <b>Имя:</b> {escape(r[4])}\n{emoji("user","👤")} <b>Telegram:</b> {escape(r[5])}\n{emoji("manager","🤝")} <b>Менеджер:</b> {escape(r[6])}\n{status(r[7])}\n\n<b>Создана:</b> {escape(r[1])}'
    await edit(q,t,actions(r[0],r[7]))
async def apps(update,context,status_filter='all',mid=None):
    if mid is None and update.effective_user.id not in ADMIN_IDS:mid=update.effective_user.id
    cnd=[];args=[]
    if status_filter!='all':cnd.append('status=?');args.append(status_filter)
    if mid is not None:cnd.append('manager_id=?');args.append(mid)
    w=(' WHERE '+' AND '.join(cnd)) if cnd else ''
    with db() as c:rows=c.execute(f'SELECT id,product,bank,client_name,manager,status FROM applications{w} ORDER BY id DESC LIMIT 30',args).fetchall()
    back='admin' if update.effective_user.id in ADMIN_IDS else 'manager_panel'; kb=[[btn(f'Открыть #{r[0]}','open',f'view:{r[0]}')] for r in rows]+[[btn('Назад','back',back)]]
    t=emoji('apps','📋')+' <b>Заявки</b>\n\n'+('\n\n'.join(f'<b>#{r[0]}</b> · {escape(r[3])}\n{escape(PRODUCTS[r[1]])} · {escape(r[2])}\n{escape(r[4])} · {escape(r[5])}' for r in rows) if rows else 'Заявок пока нет.')
    if update.callback_query:await edit(update.callback_query,t,InlineKeyboardMarkup(kb))
    else:await update.message.reply_text(t,parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(kb))
async def report(bot,mid=None):
    q='SELECT manager,COUNT(*),SUM(status="completed") FROM applications WHERE created_at>=datetime("now","-7 day")'+(' AND manager_id=?' if mid else '')+' GROUP BY manager';args=(mid,) if mid else ()
    with db() as c:r=c.execute(q,args).fetchall()
    t=emoji('report','📈')+' <b>Отчёт Media Bank за 7 дней</b>\n\n'+(''.join(f'• {escape(x[0])}: {x[1]} заявок, {x[2] or 0} завершено\n' for x in r) if r else 'Заявок за период нет.')
    await bot.send_message(REPORT_CHAT_ID,t,parse_mode=ParseMode.HTML)
async def export_csv(update,context):
    if update.effective_user.id not in ADMIN_IDS:return
    with db() as c:r=c.execute('SELECT id,created_at,product,bank,client_name,contact,manager,status FROM applications ORDER BY id DESC').fetchall()
    s=io.StringIO();w=csv.writer(s);w.writerow(['ID','Created','Product','Bank','Client','Telegram','Manager','Status']);w.writerows(r);f=io.BytesIO(s.getvalue().encode('utf-8-sig'));f.name='media_bank_applications.csv';await update.effective_message.reply_document(f,caption='Экспорт заявок Media Bank')
async def callback(update,context):
    q=update.callback_query;await q.answer();d=q.data;u=q.from_user
    if d=='home':return await start(update,context)
    if d=='form':
        context.user_data.clear();await edit(q,emoji('form','📝')+' <b>Выберите продукт:</b>',InlineKeyboardMarkup([[btn('Дебетовая карта','debit','product:debit')],[btn('Кредитная карта','credit','product:credit')],[btn('Регистрация бизнеса + РКО','rko','product:rko')],[btn('Назад','back','home')]]));return
    if d.startswith('product:'):
        p=d.split(':')[1];context.user_data['product']=p;await edit(q,emoji(p,'•')+f' <b>{PRODUCTS[p]}</b>\n\nВыберите банк:',bank_kb(p));return
    if d.startswith('banks:'):
        p=d.split(':')[1];context.user_data['product']=p;await edit(q,emoji('bank','🏦')+' <b>Выберите банк:</b>',bank_kb(p));return
    if d.startswith('bank:'):
        _,p,i=d.split(':');context.user_data.update(product=p,bank=BANKS[p][int(i)],step='name');await edit(q,emoji('user','👤')+' <b>Введите имя клиента:</b>',form_kb());return
    if d=='edit':context.user_data['step']='name';await edit(q,emoji('edit','✏️')+' <b>Введите имя клиента заново:</b>',form_kb());return
    if d=='cancel':context.user_data.clear();return await start(update,context)
    if d.startswith('mgr:'):
        m=int(d.split(':')[1]);context.user_data.update(manager_id=m,manager=MANAGERS[m],step='review');x=context.user_data;t=emoji('form','📝')+' <b>Проверьте заявку</b>\n\n'+f'{emoji(x["product"],"•")} <b>Продукт:</b> {PRODUCTS[x["product"]]}\n{emoji("bank","🏦")} <b>Банк:</b> {escape(x["bank"])}\n{emoji("user","👤")} <b>Имя:</b> {escape(x["client_name"])}\n{emoji("user","👤")} <b>Telegram:</b> {escape(x["contact"])}\n{emoji("manager","🤝")} <b>Менеджер:</b> {x["manager"]}';await edit(q,t,review_kb());return
    if d=='submit':return await submit(update,context)
    if d=='admin' and u.id in ADMIN_IDS:return await edit(q,emoji('admin','⚙️')+' <b>Админ-панель</b>',admin_kb())
    if d=='manager_panel' and u.id in MANAGERS:return await edit(q,emoji('manager','🤝')+' <b>Панель менеджера</b>',manager_panel())
    if d.startswith('apps:'):return await apps(update,context,d.split(':')[1])
    if d=='myapps':return await apps(update,context,'all',u.id)
    if d=='stats' and u.id in ADMIN_IDS:return await edit(q,await stats_text(),admin_kb())
    if d=='mystats':return await edit(q,await stats_text(u.id),manager_panel())
    if d=='report' and u.id in ADMIN_IDS:await report(context.bot);return await q.answer('Отчёт отправлен',show_alert=True)
    if d=='myreport':await report(context.bot,u.id);return await q.answer('Отчёт отправлен',show_alert=True)
    if d=='export':return await export_csv(update,context)
    if d=='managers' and u.id in ADMIN_IDS:
        return await edit(q,emoji('manager','🤝')+' <b>Менеджеры</b>',InlineKeyboardMarkup([[btn(n,'manager',f'mstats:{m}')] for m,n in MANAGERS.items()]+[[btn('Назад','back','admin')]]))
    if d.startswith('mstats:'):return await edit(q,await stats_text(int(d.split(':')[1])),InlineKeyboardMarkup([[btn('Назад','back','managers')]]))
    if d.startswith('view:'):return await view(q,int(d.split(':')[1]))
    if d.startswith('take:') and u.id in MANAGERS:
        a=int(d.split(':')[1]);
        with db() as c:c.execute("UPDATE applications SET status='in_work',taken_at=? WHERE id=? AND status='new'",(now(),a));c.commit()
        return await view(q,a)
    if d.startswith('complete:') and u.id in MANAGERS:
        a=int(d.split(':')[1]);
        with db() as c:c.execute("UPDATE applications SET status='completed',completed_at=? WHERE id=? AND manager_id=?",(now(),a,u.id));c.commit()
        return await view(q,a)
async def stats_cmd(update,context):
    u=update.effective_user
    if u.id in ADMIN_IDS:await update.message.reply_text(await stats_text(),parse_mode=ParseMode.HTML,reply_markup=admin_kb())
    elif u.id in MANAGERS:await update.message.reply_text(await stats_text(u.id),parse_mode=ParseMode.HTML,reply_markup=manager_panel())
async def report_cmd(update,context):
    if update.effective_user.id in ADMIN_IDS:await report(context.bot);await update.message.reply_text('Отчёт отправлен в рабочую группу.')
async def scheduled(context):
    d=datetime.now(timezone.utc)
    if d.hour==DAILY_REPORT_HOUR and d.minute==DAILY_REPORT_MINUTE:await report(context.bot)
    if d.weekday()==WEEKLY_REPORT_DAY and d.hour==WEEKLY_REPORT_HOUR and d.minute==WEEKLY_REPORT_MINUTE:await report(context.bot)
async def setup(app):
    await app.bot.set_my_commands([BotCommand('start','Главное меню'),BotCommand('stats','Статистика'),BotCommand('report','Отчёт')]);await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
def main():
    if not BOT_TOKEN:raise RuntimeError('BOT_TOKEN is missing in .env')
    if not GROUP_ID:raise RuntimeError('GROUP_ID is missing in .env')
    init_db();app=Application.builder().token(BOT_TOKEN).post_init(setup).build();app.add_handler(CommandHandler('start',start));app.add_handler(CommandHandler('stats',stats_cmd));app.add_handler(CommandHandler('report',report_cmd));app.add_handler(CallbackQueryHandler(callback));app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_input));
    if app.job_queue:app.job_queue.run_repeating(scheduled,interval=60,first=10)
    print('MEDIA BANK — CLEAN FROM ZERO');print('DB:',DB_FILE);print('Premium message emoji: enabled');print('Premium button icons:',ENABLE_BUTTON_PREMIUM);app.run_polling(drop_pending_updates=True)
if __name__=='__main__':main()
