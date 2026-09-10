import os, json, smtplib
from email.message import EmailMessage
from datetime import datetime, date, timedelta
import streamlit as st
import pandas as pd
from db import init_db, connect, query, execute, verify_password, hash_password, audit, DB_PATH
from export_utils import export_all, DUOC_YELLOW, DUOC_BLACK

st.set_page_config(page_title='Calendario Académico', page_icon='📅', layout='wide')
st.markdown(f'''<style>
:root{{--duoc-yellow:{DUOC_YELLOW};--duoc-black:{DUOC_BLACK};}}
.stButton>button{{border-radius:8px;font-weight:600}}
div[data-testid="stMetric"]{{border:1px solid #e5e5e5;border-top:4px solid {DUOC_YELLOW};padding:10px;border-radius:8px}}
[data-testid="stSidebar"]{{background:#fafafa}}
h1,h2,h3{{color:{DUOC_BLACK}}}
</style>''',unsafe_allow_html=True)
init_db()

FIELD_LABEL={'start_date':'Inicio','end_date':'Término'}

def notify(user_id,title,message):
    execute('INSERT INTO notifications(user_id,title,message,created_at) VALUES(?,?,?,?)',(user_id,title,message,datetime.now().isoformat(timespec='seconds')))
    u=query('SELECT email FROM users WHERE id=?',(user_id,))
    if u: send_email(u[0]['email'],title,message)

def _setting(name, default=None):
    # Compatible con variables de entorno (local) y Secrets de Streamlit Cloud.
    value=os.getenv(name)
    if value is not None:
        return value
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default

def send_email(to,subject,body):
    host=_setting('SMTP_HOST'); user=_setting('SMTP_USER'); pwd=_setting('SMTP_PASSWORD'); sender=_setting('SMTP_FROM',user or '')
    if not all([host,user,pwd,sender]): return False
    try:
        msg=EmailMessage(); msg['From']=sender; msg['To']=to; msg['Subject']=subject; msg.set_content(body)
        port=int(_setting('SMTP_PORT','587'))
        with smtplib.SMTP(host,port,timeout=10) as s:
            s.starttls(); s.login(user,pwd); s.send_message(msg)
        return True
    except Exception: return False

def login():
    st.title('📅 Gestión de Calendario Académico')
    st.caption('Maqueta funcional · validación colaborativa y versionamiento')
    with st.form('login'):
        email=st.text_input('Correo'); pwd=st.text_input('Contraseña',type='password'); ok=st.form_submit_button('Ingresar',type='primary')
    if ok:
        rows=query('SELECT * FROM users WHERE lower(email)=lower(?) AND active=1',(email.strip(),))
        if rows and verify_password(pwd,rows[0]['password_hash']): st.session_state.user=dict(rows[0]); st.rerun()
        st.error('Credenciales incorrectas.')
    with st.expander('Usuarios de demostración'):
        st.code('admin@demo.cl / Admin123!\nlider@demo.cl / Lider123!\nvisual@demo.cl / Visual123!')

def d(v):
    if not v: return None
    return datetime.strptime(v,'%Y-%m-%d').date()

def s(v): return v.isoformat() if isinstance(v,date) else v

def rule_slack(rule, override_occ=None, new_start=None, new_end=None):
    src=query('SELECT * FROM occurrences WHERE id=?',(rule['source_occurrence_id'],))[0]
    tgt=query('SELECT * FROM occurrences WHERE id=?',(rule['target_occurrence_id'],))[0]
    def value(o,field):
        if override_occ and o['id']==override_occ:
            return new_start if field=='start_date' else new_end
        return d(o[field])
    sv=value(src,rule['source_field']); tv=value(tgt,rule['target_field'])
    if not sv or not tv: return None
    delta=(tv-sv).days
    rule_type=(rule['rule_type'] if 'rule_type' in rule.keys() else None) or ('antes' if rule['operator']=='<=' else 'después')
    if rule_type=='igual':
        # Cumplimiento exacto: 0 cuando las fechas son iguales; negativo cuando existe diferencia.
        return -abs(delta)
    if rule_type=='antes':
        # target <= source - N días. offset_days se almacena negativo para esta regla.
        return rule['offset_days']-delta
    # después: target >= source + N días.
    return delta-rule['offset_days']

def dependency_impacts(occ_id,new_start,new_end):
    rules=query('SELECT * FROM dependencies WHERE active=1 AND (source_occurrence_id=? OR target_occurrence_id=?)',(occ_id,occ_id))
    impacts=[]
    for r in rules:
        old=rule_slack(r); new=rule_slack(r,occ_id,new_start,new_end)
        if old is None or new is None: continue
        if new < old or new < 0:
            impacted_occ=r['target_occurrence_id'] if r['source_occurrence_id']==occ_id else r['source_occurrence_id']
            o=query('''SELECT o.id,o.activity_id,o.semester,a.code,a.name FROM occurrences o JOIN activities a ON a.id=o.activity_id WHERE o.id=?''',(impacted_occ,))[0]
            impacts.append({'dependency_id':r['id'],'impacted_occurrence_id':impacted_occ,'activity_id':o['activity_id'],'code':o['code'],'name':o['name'],'semester':o['semester'],'old_slack':old,'new_slack':new,'description':r['description'] or ''})
    return impacts

def snapshot_version(version_id):
    """Guarda una fotografía inmutable de las fechas que componen una versión."""
    execute("""INSERT OR REPLACE INTO version_occurrences(version_id,occurrence_id,activity_id,semester,start_date,end_date,period_label)
               SELECT ?,id,activity_id,semester,start_date,end_date,period_label FROM occurrences""",(version_id,))

def create_initial_version(user, year):
    existing=query("SELECT id FROM versions WHERE year=? AND status='PUBLICADA' ORDER BY version_no LIMIT 1",(year,))
    if existing: return None
    now=datetime.now().isoformat(timespec='seconds')
    vid=execute("""INSERT INTO versions(year,version_no,status,change_id,created_at,published_at,source,description)
                   VALUES(?,1,'PUBLICADA',NULL,?,?,?,?)""",(year,now,now,'Carga inicial Excel','Versión base generada desde el calendario cargado originalmente.'))
    snapshot_version(vid)
    paths=export_all(DB_PATH,year,1,'exports',version_id=vid)
    audit(user['id'],'GENERAR_VERSION_INICIAL','versions',vid,'; '.join(paths))
    return vid

def create_draft_version(change_id):
    ch=query('SELECT o.semester FROM changes c JOIN occurrences o ON o.id=c.occurrence_id WHERE c.id=?',(change_id,))[0]
    # El año se obtiene del semestre (ej. 2027-1) para soportar calendarios futuros.
    try:
        year=int(str(ch['semester']).split('-')[0])
    except Exception:
        year=datetime.now().year
    nums=query('SELECT COALESCE(MAX(version_no),0)+1 n FROM versions WHERE year=?',(year,)); n=nums[0]['n']
    try: execute('INSERT INTO versions(year,version_no,status,change_id,created_at) VALUES(?,?,?,?,?)',(year,n,'BORRADOR',change_id,datetime.now().isoformat(timespec='seconds')))
    except Exception: pass

def propose_change(user,occ_id,new_start,new_end):
    o=query('SELECT * FROM occurrences WHERE id=?',(occ_id,))[0]
    impacts=dependency_impacts(occ_id,new_start,new_end)
    now=datetime.now().isoformat(timespec='seconds')
    status='Pendiente líderes' if impacts else 'Borrador pendiente administrador'
    cid=execute('''INSERT INTO changes(occurrence_id,requested_by,old_start,old_end,new_start,new_end,status,created_at,updated_at,impact_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?)''',(occ_id,user['id'],o['start_date'],o['end_date'],s(new_start),s(new_end),status,now,now,json.dumps(impacts,ensure_ascii=False)))
    approvers={}
    for imp in impacts:
        for a in query('''SELECT u.id,u.first_name,u.last_name FROM assignments x JOIN users u ON u.id=x.user_id WHERE x.activity_id=? AND u.role='Líder' AND u.active=1''',(imp['activity_id'],)):
            if a['id']!=user['id']: approvers[a['id']]=imp['impacted_occurrence_id']
    for uid,ioid in approvers.items():
        execute('INSERT OR IGNORE INTO approvals(change_id,user_id,impacted_occurrence_id) VALUES(?,?,?)',(cid,uid,ioid))
        notify(uid,'Cambio de calendario requiere validación',f'El cambio #{cid} reduce la holgura de una actividad bajo su responsabilidad. Ingrese a la plataforma para revisar.')
    if impacts and not approvers:
        execute("UPDATE changes SET status='Borrador pendiente administrador' WHERE id=?",(cid,)); create_draft_version(cid)
    if not impacts: create_draft_version(cid)
    audit(user['id'],'PROPONER_CAMBIO','changes',cid,f'{o["start_date"]}/{o["end_date"]} -> {s(new_start)}/{s(new_end)}')
    return cid,impacts

MONTHS_ES={1:'Enero',2:'Febrero',3:'Marzo',4:'Abril',5:'Mayo',6:'Junio',7:'Julio',8:'Agosto',9:'Septiembre',10:'Octubre',11:'Noviembre',12:'Diciembre'}

def _is_nonworking(value):
    # Domingos y fechas configuradas como feriado se consideran no hábiles.
    if value is None or value=='': return False
    dt=d(value) if isinstance(value,str) else value
    if hasattr(dt,'date') and not isinstance(dt,date): dt=dt.date()
    if not isinstance(dt,date): return False
    if dt.weekday()==6: return True
    rows=query("SELECT day_type,holiday_type FROM holidays WHERE day=? AND month=? AND (holiday_type='Permanente' OR (holiday_type='Variable' AND year=?)) ORDER BY CASE WHEN holiday_type='Permanente' THEN 0 ELSE 1 END",(dt.day,dt.month,dt.year))
    if not rows: return False
    for r in rows:
        if r['holiday_type']=='Permanente' and r['day_type']=='Feriado': return True
    return any(r['day_type']=='Feriado' for r in rows)

def _style_calendar_dates(df, columns):
    def style(v):
        try: return 'color:#d00000;font-weight:700' if _is_nonworking(v) else ''
        except Exception: return ''
    sty=df.style
    for col in columns:
        if col in df.columns: sty=sty.map(style,subset=[col])
    return sty

def _nearest_same_weekday(src_date, target_year):
    # Ajusta al año destino cerca del mismo día/mes y conserva el día de semana.
    if not src_date: return None
    if isinstance(src_date,str): src_date=d(src_date)
    day=src_date.day
    while day>0:
        try:
            base=date(int(target_year),src_date.month,day); break
        except ValueError: day-=1
    wanted=src_date.weekday()
    forward=(wanted-base.weekday())%7
    candidates=[base+timedelta(days=forward)]
    backward=forward-7 if forward else 0
    candidates.append(base+timedelta(days=backward))
    candidates=[x for x in candidates if x.year==int(target_year)]
    return min(candidates,key=lambda x:abs((x-base).days)) if candidates else base

def _calendar_years():
    years={int(r['year']) for r in query('SELECT DISTINCT year FROM periods') if r['year']}
    for r in query('SELECT DISTINCT semester FROM occurrences'):
        txt=str(r['semester'])
        if len(txt)>=4 and txt[:4].isdigit(): years.add(int(txt[:4]))
    return sorted(years)

def _semester_for_year(semester, source_year, target_year):
    txt=str(semester)
    if txt=='TAV' or txt==f'{source_year}-TAV': return f'{target_year}-TAV'
    if txt.startswith(str(source_year)): return str(target_year)+txt[4:]
    return txt

def copy_calendar_year(user, source_year, target_year):
    source_year=int(source_year); target_year=int(target_year)
    if source_year==target_year: return False,'El año destino debe ser distinto del calendario de origen.'
    exists=query('SELECT COUNT(*) n FROM periods WHERE year=?',(target_year,))[0]['n']
    if exists: return False,f'Ya existe un calendario o periodos configurados para {target_year}.'
    source_periods=query('SELECT * FROM periods WHERE year=? ORDER BY id',(source_year,))
    if not source_periods: return False,f'No existen periodos para el calendario {source_year}.'
    source_sems=[]
    for p in source_periods:
        sem=p['semester']
        if sem not in source_sems: source_sems.append(sem)
    source_occ=[]
    for sem in source_sems:
        source_occ += list(query('SELECT * FROM occurrences WHERE semester=? ORDER BY id',(sem,)))
    if source_year==2027 and 'TAV' not in source_sems:
        source_occ += list(query("SELECT * FROM occurrences WHERE semester='TAV' ORDER BY id"))
    for p in source_periods:
        new_sem=_semester_for_year(p['semester'],source_year,target_year)
        execute('INSERT INTO periods(year,semester,start_date,end_date) VALUES(?,?,?,?)',(
            target_year,new_sem,s(_nearest_same_weekday(p['start_date'],target_year)),s(_nearest_same_weekday(p['end_date'],target_year))))
    occ_map={}
    for o in source_occ:
        new_sem=_semester_for_year(o['semester'],source_year,target_year)
        ns=_nearest_same_weekday(o['start_date'],target_year) if o['start_date'] else None
        ne=_nearest_same_weekday(o['end_date'],target_year) if o['end_date'] else None
        oid=execute('INSERT OR IGNORE INTO occurrences(activity_id,semester,start_date,end_date,period_label) VALUES(?,?,?,?,?)',
                    (o['activity_id'],new_sem,s(ns),s(ne),o['period_label']))
        if not oid:
            rr=query('SELECT id FROM occurrences WHERE activity_id=? AND semester=?',(o['activity_id'],new_sem)); oid=rr[0]['id'] if rr else None
        if oid: occ_map[o['id']]=oid
    deps=query('SELECT * FROM dependencies WHERE active=1')
    for dep in deps:
        if dep['source_occurrence_id'] in occ_map and dep['target_occurrence_id'] in occ_map:
            execute('INSERT INTO dependencies(source_occurrence_id,source_field,target_occurrence_id,target_field,operator,offset_days,description,active,rule_type) VALUES(?,?,?,?,?,?,?,?,?)',
                    (occ_map[dep['source_occurrence_id']],dep['source_field'],occ_map[dep['target_occurrence_id']],dep['target_field'],dep['operator'],dep['offset_days'],dep['description'],dep['active'],dep['rule_type']))
    audit(user['id'],'COPIAR_CALENDARIO','calendar',target_year,f'{source_year} → {target_year}; {len(occ_map)} registros')
    return True,f'Calendario {target_year} creado a partir de {source_year}. Las fechas conservaron su día de la semana y quedaron disponibles para revisión.'

def copy_calendar_page(user):
    st.title('Copiar calendario')
    st.caption('Crea un nuevo calendario a partir de uno existente y ajusta sus fechas al año destino conservando el mismo día de la semana.')
    years=_calendar_years()
    if not years:
        st.warning('No existen calendarios disponibles para copiar.'); return
    source=st.selectbox('Calendario de origen',years,format_func=lambda x:f'Calendario {x}')
    target_options=[y for y in range(max(years)+1,max(years)+11) if y not in years]
    if not target_options:
        st.warning('No hay años destino disponibles.'); return
    target=st.selectbox('Año del nuevo calendario',target_options)
    st.info(f'Se copiará el Calendario {source} hacia {target}. Cada fecha se ubicará cerca del mismo día y mes, manteniendo exactamente el mismo día de la semana del calendario original.')
    if st.button(f'Crear calendario {target}',type='primary'):
        ok,msg=copy_calendar_year(user,source,target)
        (st.success if ok else st.error)(msg)
        if ok: st.rerun()

def holidays_admin(user):
    st.title('Mantenedor de feriados')
    st.caption('Los feriados permanentes aplican a todos los años. Los feriados variables aplican solo al año indicado. Todos los días no informados como feriado se consideran hábiles y los domingos se consideran siempre no hábiles.')
    rows=query('SELECT * FROM holidays ORDER BY CASE WHEN holiday_type="Permanente" THEN 0 ELSE 1 END, year,month,day')
    if rows:
        df=pd.DataFrame([{
            'ID':r['id'],
            'Año':'Todos' if r['holiday_type']=='Permanente' else r['year'],
            'Día':r['day'],
            'Mes':MONTHS_ES[r['month']],
            'Tipo de día':r['day_type'],
            'Tipo de feriado':r['holiday_type']
        } for r in rows])
        st.dataframe(df,hide_index=True,use_container_width=True)
    else:
        st.info('No hay días configurados.')

    cal_years=_calendar_years()
    default_year=(max(cal_years) if cal_years else date.today().year)
    tabs=st.tabs(['Agregar','Modificar','Eliminar'])

    with tabs[0]:
        with st.form('holiday_add'):
            c0,c1,c2,c3,c4=st.columns([1.1,0.8,1.4,1.2,1.4])
            year=c0.number_input('Año',min_value=2000,max_value=2100,value=int(default_year),step=1,help='Para feriados permanentes el año se ignora.')
            day=c1.number_input('Día',1,31,1)
            month=c2.selectbox('Mes',list(MONTHS_ES),format_func=lambda x:MONTHS_ES[x])
            dtype=c3.selectbox('Tipo de día',['Feriado','Hábil'])
            holiday_type=c4.selectbox('Tipo de feriado',['Variable','Permanente'],key='holiday_type_add',help='Permanente: aplica a todos los años. Variable: aplica solo al año seleccionado.')
            st.caption('Si el tipo de feriado es Permanente, el año se ignora y la fecha aplicará a todos los calendarios.')
            if st.form_submit_button('Agregar día',type='primary'):
                store_year=0 if holiday_type=='Permanente' else int(year)
                validation_year=2028 if holiday_type=='Permanente' else int(year)
                try:
                    date(validation_year,int(month),int(day))
                    hid=execute('INSERT INTO holidays(year,day,month,day_type,holiday_type) VALUES(?,?,?,?,?)',(store_year,int(day),int(month),dtype,holiday_type))
                    audit(user['id'],'AGREGAR_FERIADO','holidays',hid,f"{day}/{month}/"+('Todos' if holiday_type=='Permanente' else str(year))+f" {dtype} {holiday_type}")
                    st.rerun()
                except ValueError:
                    st.error('La combinación de día y mes indicada no corresponde a una fecha válida.')
                except Exception:
                    st.error('Ese feriado ya está configurado. Use Modificar para cambiarlo.')

    hl={r['id']:f"{r['day']:02d}/{r['month']:02d}/"+('Todos' if r['holiday_type']=='Permanente' else str(r['year']))+f" · {r['day_type']} · {r['holiday_type']}" for r in rows}
    with tabs[1]:
        if hl:
            hid=st.selectbox('Día a modificar',list(hl),format_func=lambda x:hl[x],key='holiday_edit')
            r=next(x for x in rows if x['id']==hid)
            with st.form('holiday_edit_form'):
                c0,c1,c2,c3,c4=st.columns([1.1,0.8,1.4,1.2,1.4])
                base_year=int(r['year']) if int(r['year'])>0 else int(default_year)
                year=c0.number_input('Año',min_value=2000,max_value=2100,value=base_year,step=1,key='hyear',help='Para feriados permanentes el año se ignora.')
                day=c1.number_input('Día',1,31,int(r['day']),key='hday')
                months=list(MONTHS_ES)
                month=c2.selectbox('Mes',months,index=months.index(r['month']),format_func=lambda x:MONTHS_ES[x],key='hmonth')
                types=['Feriado','Hábil']
                dtype=c3.selectbox('Tipo de día',types,index=types.index(r['day_type']),key='htype')
                type_options=['Variable','Permanente']
                holiday_type=c4.selectbox('Tipo de feriado',type_options,index=type_options.index(r['holiday_type']),key='holiday_type_edit')
                st.caption('Si el tipo de feriado es Permanente, el año se ignora y la fecha aplicará a todos los calendarios.')
                if st.form_submit_button('Guardar cambios',type='primary'):
                    store_year=0 if holiday_type=='Permanente' else int(year)
                    validation_year=2028 if holiday_type=='Permanente' else int(year)
                    try:
                        date(validation_year,int(month),int(day))
                        execute('UPDATE holidays SET year=?,day=?,month=?,day_type=?,holiday_type=? WHERE id=?',(store_year,int(day),int(month),dtype,holiday_type,hid))
                        audit(user['id'],'MODIFICAR_FERIADO','holidays',hid,f"{day}/{month}/"+('Todos' if holiday_type=='Permanente' else str(year))+f" {dtype} {holiday_type}")
                        st.rerun()
                    except ValueError:
                        st.error('La combinación de día y mes indicada no corresponde a una fecha válida.')
                    except Exception:
                        st.error('Ya existe otro registro con esa configuración.')

    with tabs[2]:
        if hl:
            hid=st.selectbox('Día a eliminar',list(hl),format_func=lambda x:hl[x],key='holiday_delete')
            if st.button('Eliminar día',type='primary'):
                execute('DELETE FROM holidays WHERE id=?',(hid,))
                audit(user['id'],'ELIMINAR_FERIADO','holidays',hid,'')
                st.rerun()

def sidebar(user):
    st.sidebar.markdown('## Calendario Académico')
    st.sidebar.caption(f"{user['first_name']} {user['last_name']} · {user['role']}")
    if user['role']=='Visualizador':
        opts=['Versiones']
    elif user['role']=='Administrador':
        opts=['Inicio','Calendario','Mis validaciones','Notificaciones','Versiones','Copiar calendario','Usuarios','Actividades','Periodos','Feriados','Dependencias','Asignaciones','Auditoría']
    else:
        opts=['Inicio','Calendario','Mis validaciones','Notificaciones','Versiones']
    if 'nav_page' not in st.session_state or st.session_state.nav_page not in opts:
        st.session_state.nav_page=opts[0]
    page=st.sidebar.radio('Navegación',opts,key='nav_page')
    if st.sidebar.button('Cerrar sesión'):
        st.session_state.clear(); st.rerun()
    return page

def dashboard(user):
    st.title('Inicio')
    pend=query("SELECT COUNT(*) n FROM approvals WHERE user_id=? AND status='Pendiente'",(user['id'],))[0]['n']
    notif=query('SELECT COUNT(*) n FROM notifications WHERE user_id=? AND read_at IS NULL',(user['id'],))[0]['n']
    pub=query("SELECT version_no,published_at FROM versions WHERE status='PUBLICADA' ORDER BY year DESC,version_no DESC LIMIT 1")
    own=query('SELECT COUNT(*) n FROM assignments WHERE user_id=?',(user['id'],))[0]['n'] if user['role']=='Líder' else query('SELECT COUNT(*) n FROM activities WHERE active=1')[0]['n']
    c1,c2,c3,c4=st.columns(4)
    with c1:
        st.caption('Actividades asignadas' if user['role']=='Líder' else 'Actividades activas')
        if user['role']=='Líder':
            def _open_assigned():
                st.session_state.calendar_assigned_only=True
                st.session_state.nav_page='Calendario'
            st.button(str(own),key='open_assigned',use_container_width=True,help='Abrir mis actividades asignadas',on_click=_open_assigned)
        else:
            st.metric('',own,label_visibility='collapsed')
    c2.metric('Validaciones pendientes',pend)
    c3.metric('Notificaciones nuevas',notif)
    c4.metric('Última versión',f"V{pub[0]['version_no']}" if pub else 'Sin publicar')
    if user['role']=='Líder':
        st.caption('Pulse el número de **Actividades asignadas** para abrir directamente las actividades bajo su responsabilidad.')
    st.info('Los líderes pueden editar únicamente las fechas de las actividades asignadas. Las dependencias se revisan automáticamente y solo se activa aprobación cuando disminuye la holgura o se incumple una regla.')

def _calendar_rows(user, semester='Todos', group='Todos', assigned_only=False):
    sql="""SELECT o.id,a.id activity_id,a.code,g.name grupo,a.name,o.semester,o.start_date,o.end_date,
             oc.comment,oc.user_id comment_user_id,oc.created_at comment_created_at,oc.updated_at comment_updated_at,
             cu.first_name||' '||cu.last_name comment_author
             FROM occurrences o JOIN activities a ON a.id=o.activity_id JOIN activity_groups g ON g.id=a.group_id
             LEFT JOIN occurrence_comments oc ON oc.occurrence_id=o.id LEFT JOIN users cu ON cu.id=oc.user_id
             WHERE a.active=1"""
    params=[]
    if semester!='Todos':
        sql+=' AND o.semester=?'; params.append(semester)
    if group!='Todos':
        sql+=' AND g.name=?'; params.append(group)
    if user['role']=='Líder' and assigned_only:
        sql+=' AND EXISTS (SELECT 1 FROM assignments x WHERE x.user_id=? AND x.activity_id=a.id)'; params.append(user['id'])
    return query(sql+' ORDER BY g.id,a.id,o.semester',params)

def _save_comment(user, occurrence_id, text):
    existing=query('SELECT * FROM occurrence_comments WHERE occurrence_id=?',(occurrence_id,))
    now=datetime.now().isoformat(timespec='seconds')
    text=(text or '').strip()
    if existing:
        e=existing[0]
        if e['user_id']!=user['id']:
            return False,'El comentario pertenece a otro usuario y solo su autor puede modificarlo o eliminarlo.'
        if text:
            execute('UPDATE occurrence_comments SET comment=?,updated_at=? WHERE id=?',(text,now,e['id']))
            audit(user['id'],'MODIFICAR_COMENTARIO','occurrence_comments',e['id'],text)
        else:
            execute('DELETE FROM occurrence_comments WHERE id=?',(e['id'],))
            audit(user['id'],'ELIMINAR_COMENTARIO','occurrence_comments',e['id'],'')
    elif text:
        cid=execute('INSERT INTO occurrence_comments(occurrence_id,user_id,comment,created_at,updated_at) VALUES(?,?,?,?,?)',(occurrence_id,user['id'],text,now,now))
        audit(user['id'],'AGREGAR_COMENTARIO','occurrence_comments',cid,text)
    return True,''

def calendar_page(user):
    st.title('Calendario')
    semesters=['Todos']+[r['semester'] for r in query('SELECT DISTINCT semester FROM occurrences ORDER BY semester')]
    groups=['Todos']+[r['name'] for r in query('SELECT name FROM activity_groups ORDER BY id')]
    f1,f2,f3=st.columns([1,1,1])
    sem=f1.selectbox('Semestre',semesters,key='calendar_semester')
    grp=f2.selectbox('Grupo',groups,key='calendar_group')
    assigned_only=False
    if user['role']=='Líder':
        if 'calendar_assigned_only' not in st.session_state:
            st.session_state.calendar_assigned_only=False
        assigned_only=f3.toggle('Actividades asignadas',key='calendar_assigned_only',help='Muestra únicamente las actividades asignadas a su usuario.')
    else:
        f3.caption('El Administrador puede editar todas las actividades sin asignación previa.')

    rows=_calendar_rows(user,sem,grp,assigned_only)
    if not rows:
        st.warning('No existen actividades para los filtros seleccionados.'); return
    assigned={r['activity_id'] for r in query('SELECT activity_id FROM assignments WHERE user_id=?',(user['id'],))} if user['role']=='Líder' else set()
    can_inline_edit = user['role']=='Administrador' or (user['role']=='Líder' and assigned_only)

    display=[]
    for r in rows:
        display.append({'occurrence_id':r['id'],'activity_id':r['activity_id'],'comment_user_id':r['comment_user_id'],
                        'ID':r['code'],'Grupo':r['grupo'],'Actividad':r['name'],'Semestre':r['semester'],
                        'Fecha inicio':d(r['start_date']),'Fecha término':d(r['end_date']),
                        'Comentario':r['comment'] or '','Autor comentario':r['comment_author'] or ''})
    df=pd.DataFrame(display)

    if not can_inline_edit:
        view=df.drop(columns=['occurrence_id','activity_id','comment_user_id'])
        st.dataframe(_style_calendar_dates(view,['Fecha inicio','Fecha término']),hide_index=True,use_container_width=True,
                     column_config={'Fecha inicio':st.column_config.DateColumn(format='DD/MM/YYYY'),'Fecha término':st.column_config.DateColumn(format='DD/MM/YYYY')})
        if user['role']=='Líder':
            st.caption('Active **Actividades asignadas** para editar en línea las fechas bajo su responsabilidad.')
        return

    editable=df.copy()
    editable.insert(editable.columns.get_loc('Fecha término')+1,'Día no hábil',editable.apply(lambda r:'🔴' if _is_nonworking(r['Fecha inicio']) or _is_nonworking(r['Fecha término']) else '',axis=1))
    disabled_cols=['occurrence_id','activity_id','comment_user_id','ID','Grupo','Actividad','Semestre','Día no hábil','Autor comentario']
    edited=st.data_editor(editable,hide_index=True,use_container_width=True,key='calendar_editor',
        disabled=disabled_cols,
        column_config={
            'occurrence_id':None,'activity_id':None,'comment_user_id':None,
            'Fecha inicio':st.column_config.DateColumn('Fecha inicio',format='DD/MM/YYYY',required=True),
            'Fecha término':st.column_config.DateColumn('Fecha término',format='DD/MM/YYYY',required=True),
            'Comentario':st.column_config.TextColumn('Comentario',help='Visible para líderes y Administrador. Cada usuario puede crear, modificar y eliminar únicamente sus propios comentarios.'),
            'Autor comentario':st.column_config.TextColumn('Autor comentario')})

    date_changes=[]; comment_changes=[]; unauthorized_comments=[]
    for i in range(len(editable)):
        old=editable.iloc[i]; new=edited.iloc[i]
        ns=new['Fecha inicio']; ne=new['Fecha término']
        if hasattr(ns,'date'): ns=ns.date()
        if hasattr(ne,'date'): ne=ne.date()
        if ns!=old['Fecha inicio'] or ne!=old['Fecha término']:
            if ne < ns:
                st.error(f"{old['ID']}: la fecha de término no puede ser anterior a la fecha de inicio.")
            else:
                date_changes.append((int(old['occurrence_id']),old['ID'],old['Actividad'],ns,ne))
        if user['role'] in ('Líder','Administrador'):
            old_comment=str(old['Comentario'] or '')
            new_comment=str(new['Comentario'] or '')
            if new_comment!=old_comment:
                owner=old['comment_user_id']
                if pd.isna(owner): owner=None
                if owner is None or int(owner)==user['id']:
                    comment_changes.append((int(old['occurrence_id']),new_comment))
                else:
                    unauthorized_comments.append(old['ID'])

    if unauthorized_comments:
        st.error('No puede modificar comentarios creados por otro usuario: '+', '.join(unauthorized_comments)+'. Los cambios en esos comentarios no serán guardados.')
    if date_changes or comment_changes:
        st.warning(f'Hay {len(date_changes)} cambio(s) de fecha y {len(comment_changes)} cambio(s) de comentario sin guardar.')
        if st.button('Guardar cambios',type='primary'):
            for oid,code,name,ns,ne in date_changes:
                propose_change(user,oid,ns,ne)
            for oid,text in comment_changes:
                _save_comment(user,oid,text)
            st.success('Cambios guardados. Las modificaciones de fechas fueron enviadas al flujo de validación correspondiente.'); st.rerun()
    if user['role']=='Líder':
        st.caption('Para eliminar un comentario propio, borre su contenido en la celda **Comentario** y pulse **Guardar cambios**. Los comentarios de otros usuarios son de solo lectura.')
    else:
        st.caption('El Administrador también puede crear, modificar y eliminar sus propios comentarios. Los comentarios creados por otros usuarios son de solo lectura.')

def validations(user):
    st.title('Mis validaciones')
    rows=query('''SELECT ap.id approval_id,ap.change_id,ap.status,c.old_start,c.old_end,c.new_start,c.new_end,a.code,a.name,o.semester
                  FROM approvals ap JOIN changes c ON c.id=ap.change_id JOIN occurrences o ON o.id=c.occurrence_id JOIN activities a ON a.id=o.activity_id
                  WHERE ap.user_id=? ORDER BY ap.id DESC''',(user['id'],))
    if not rows: st.info('No hay validaciones asignadas.'); return
    for r in rows:
        with st.expander(f"Cambio #{r['change_id']} · {r['code']} · {r['name']} · {r['status']}"):
            st.write(f"**Semestre:** {r['semester']}  ·  **Inicio:** {r['old_start']} → {r['new_start']}  ·  **Término:** {r['old_end']} → {r['new_end']}")
            if r['status']=='Pendiente':
                comment=st.text_input('Comentario',key=f'com{r["approval_id"]}')
                c1,c2=st.columns(2)
                if c1.button('Aprobar',key=f'a{r["approval_id"]}',type='primary'):
                    execute("UPDATE approvals SET status='Aprobado',comment=?,decided_at=? WHERE id=?",(comment,datetime.now().isoformat(timespec='seconds'),r['approval_id']))
                    left=query("SELECT COUNT(*) n FROM approvals WHERE change_id=? AND status='Pendiente'",(r['change_id'],))[0]['n']
                    rejected=query("SELECT COUNT(*) n FROM approvals WHERE change_id=? AND status='Rechazado'",(r['change_id'],))[0]['n']
                    if left==0 and rejected==0:
                        execute("UPDATE changes SET status='Borrador pendiente administrador',updated_at=? WHERE id=?",(datetime.now().isoformat(timespec='seconds'),r['change_id'])); create_draft_version(r['change_id'])
                    st.rerun()
                if c2.button('Rechazar',key=f'r{r["approval_id"]}'):
                    execute("UPDATE approvals SET status='Rechazado',comment=?,decided_at=? WHERE id=?",(comment,datetime.now().isoformat(timespec='seconds'),r['approval_id']))
                    ch=query('SELECT requested_by FROM changes WHERE id=?',(r['change_id'],))[0]
                    execute("UPDATE changes SET status='Rechazado por líder',updated_at=? WHERE id=?",(datetime.now().isoformat(timespec='seconds'),r['change_id']))
                    notify(ch['requested_by'],'Cambio rechazado',f'El cambio #{r["change_id"]} fue rechazado y debe ser reformulado.'); st.rerun()

def notifications(user):
    st.title('Notificaciones')
    rows=query('SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC',(user['id'],))
    for r in rows:
        st.markdown(f"**{r['title']}**  \n{r['message']}  \n_{r['created_at']}_")
        if not r['read_at'] and st.button('Marcar como leída',key=f'n{r["id"]}'):
            execute('UPDATE notifications SET read_at=? WHERE id=?',(datetime.now().isoformat(timespec='seconds'),r['id'])); st.rerun()

def versions(user):
    st.title('Versiones del Calendario Académico')
    st.caption('Cada versión publicada conserva una fotografía de sus fechas, por lo que puede descargarse posteriormente sin alteraciones.')

    years=[r['year'] for r in query("SELECT DISTINCT CAST(substr(semester,1,4) AS INTEGER) year FROM occurrences WHERE semester GLOB '[0-9][0-9][0-9][0-9]-*' ORDER BY year DESC") if r['year']]
    if not years: years=[2027]

    if user['role']=='Administrador':
        st.subheader('Versión base')
        year=st.selectbox('Año del calendario',years,key='base_version_year')
        existing=query("SELECT * FROM versions WHERE year=? AND status='PUBLICADA' ORDER BY version_no",(year,))
        if not existing:
            st.info(f'El calendario {year} aún no tiene una versión publicada. Puedes generar la V1 con las fechas actualmente cargadas desde el Excel base.')
            if st.button(f'Generar V1 · Calendario {year}',type='primary'):
                vid=create_initial_version(user,year)
                if vid:
                    st.success(f'V1 del Calendario Académico {year} generada correctamente. Ya están disponibles los cuatro formatos de descarga.')
                    st.rerun()
        else:
            st.success(f'El calendario {year} ya cuenta con una versión base/publicada.')

    drafts=query("SELECT v.*,c.occurrence_id,c.new_start,c.new_end,a.code,a.name FROM versions v JOIN changes c ON c.id=v.change_id JOIN occurrences o ON o.id=c.occurrence_id JOIN activities a ON a.id=o.activity_id WHERE v.status='BORRADOR' ORDER BY v.year,v.version_no")
    if user['role']=='Administrador' and drafts:
        st.subheader('Borradores pendientes de aprobación final')
        for r in drafts:
            with st.expander(f"{r['year']} · V{r['version_no']} · cambio #{r['change_id']} · {r['code']} {r['name']}"):
                st.write(f"Fechas propuestas: **{r['new_start']}** a **{r['new_end']}**")
                comment=st.text_input('Comentario administrador',key=f'vc{r["id"]}')
                c1,c2=st.columns(2)
                if c1.button('Publicar versión',key=f'vp{r["id"]}',type='primary'):
                    execute('UPDATE occurrences SET start_date=?,end_date=? WHERE id=?',(r['new_start'],r['new_end'],r['occurrence_id']))
                    now=datetime.now().isoformat(timespec='seconds')
                    execute("UPDATE changes SET status='Publicado',admin_comment=?,updated_at=? WHERE id=?",(comment,now,r['change_id']))
                    execute("UPDATE versions SET status='PUBLICADA',published_at=?,source=COALESCE(source,'Cambio aprobado'),description=COALESCE(description,?) WHERE id=?",(now,f"Cambio #{r['change_id']} aprobado y publicado.",r['id']))
                    snapshot_version(r['id'])
                    paths=export_all(DB_PATH,r['year'],r['version_no'],'exports',version_id=r['id'])
                    audit(user['id'],'PUBLICAR_VERSION','versions',r['id'],'; '.join(paths)); st.success('Versión publicada y archivos generados.'); st.rerun()
                if c2.button('Rechazar borrador',key=f'vr{r["id"]}'):
                    execute("UPDATE changes SET status='Rechazado por administrador',admin_comment=?,updated_at=? WHERE id=?",(comment,datetime.now().isoformat(timespec='seconds'),r['change_id']))
                    execute("UPDATE versions SET status='RECHAZADA' WHERE id=?",(r['id'],)); st.rerun()

    pubs=query("SELECT * FROM versions WHERE status='PUBLICADA' ORDER BY year DESC,version_no DESC")
    st.subheader('Versiones publicadas')
    if not pubs:
        st.info('Aún no existen versiones publicadas.')
        return
    for v in pubs:
        source=v['source'] if 'source' in v.keys() and v['source'] else ('Carga inicial Excel' if v['version_no']==1 and v['change_id'] is None else 'Cambio aprobado')
        desc=v['description'] if 'description' in v.keys() and v['description'] else ''
        when=v['published_at'] or v['created_at']
        with st.expander(f"V{v['version_no']} · Calendario Académico {v['year']} · PUBLICADA",expanded=(v==pubs[0])):
            st.write(f"**Fecha de publicación:** {when}")
            st.write(f"**Origen:** {source}")
            if desc: st.write(desc)
            snap=query('SELECT COUNT(*) n FROM version_occurrences WHERE version_id=?',(v['id'],))[0]['n']
            if snap==0:
                # Compatibilidad con versiones antiguas creadas antes de incorporar snapshots.
                snapshot_version(v['id'])
            files=export_all(DB_PATH,v['year'],v['version_no'],'exports',version_id=v['id'])
            labels=['CA Procesos','CA Cronológico','CA Gráfico','PDF']
            cols=st.columns(4)
            for col,path,label in zip(cols,files,labels):
                with open(path,'rb') as f:
                    col.download_button(label,f.read(),file_name=os.path.basename(path),key=f"download_{v['id']}_{label}",use_container_width=True)

def _contains_text(values, text):
    q=(text or '').strip().lower()
    if not q: return True
    return any(q in str(v or '').lower() for v in values)


def users_admin(user):
    st.title('Mantenedor de usuarios')
    rows=query('SELECT id,first_name,last_name,email,unit,role,active FROM users ORDER BY last_name,first_name')
    search=st.text_input('Buscar usuario',placeholder='Escriba nombre, apellido, correo, unidad o rol...',key='users_search')
    filtered=[r for r in rows if _contains_text([r['first_name'],r['last_name'],r['email'],r['unit'],r['role']],search)]
    st.caption(f'{len(filtered)} usuario(s) encontrado(s)')
    if filtered:
        df=pd.DataFrame([dict(r) for r in filtered]).rename(columns={'id':'ID','first_name':'Nombre','last_name':'Apellido','email':'Correo','unit':'Unidad','role':'Rol','active':'Activo'})
        st.dataframe(df,hide_index=True,use_container_width=True)
    else:
        st.info('No hay usuarios que coincidan con la búsqueda.')

    tabs=st.tabs(['Agregar','Modificar','Eliminar'])
    with tabs[0]:
        with st.form('newuser'):
            c1,c2,c3=st.columns(3); fn=c1.text_input('Nombre'); ln=c2.text_input('Apellido'); email=c3.text_input('Correo')
            c4,c5,c6=st.columns(3); unit=c4.text_input('Unidad'); role=c5.selectbox('Rol',['Líder','Visualizador','Administrador']); pwd=c6.text_input('Contraseña inicial',type='password')
            if st.form_submit_button('Agregar usuario',type='primary'):
                if not fn.strip() or not ln.strip() or not email.strip() or not pwd:
                    st.error('Complete nombre, apellido, correo y contraseña.')
                else:
                    try:
                        uid=execute('INSERT INTO users(first_name,last_name,email,unit,role,password_hash) VALUES(?,?,?,?,?,?)',(fn.strip(),ln.strip(),email.strip(),unit.strip(),role,hash_password(pwd)))
                        audit(user['id'],'AGREGAR_USUARIO','users',uid,email); st.success('Usuario agregado.'); st.rerun()
                    except Exception as e: st.error(f'No fue posible agregar: {e}')
    choices={r['id']:f"{r['first_name']} {r['last_name']} · {r['email']} · {r['unit'] or ''}" for r in filtered}
    with tabs[1]:
        if choices:
            uid=st.selectbox('Usuario a modificar',list(choices),format_func=lambda x:choices[x],key='user_edit_select',help='El selector también permite buscar escribiendo parte del texto.')
            r=next(x for x in rows if x['id']==uid)
            with st.form('edituser'):
                c1,c2,c3=st.columns(3); fn=c1.text_input('Nombre',value=r['first_name']); ln=c2.text_input('Apellido',value=r['last_name']); email=c3.text_input('Correo',value=r['email'])
                c4,c5,c6=st.columns(3); unit=c4.text_input('Unidad',value=r['unit'] or ''); roles=['Líder','Visualizador','Administrador']; role=c5.selectbox('Rol',roles,index=roles.index(r['role'])); active=c6.checkbox('Activo',value=bool(r['active']))
                pwd=st.text_input('Nueva contraseña (dejar en blanco para conservar)',type='password')
                if st.form_submit_button('Guardar cambios',type='primary'):
                    try:
                        execute('UPDATE users SET first_name=?,last_name=?,email=?,unit=?,role=?,active=? WHERE id=?',(fn.strip(),ln.strip(),email.strip(),unit.strip(),role,1 if active else 0,uid))
                        if pwd: execute('UPDATE users SET password_hash=? WHERE id=?',(hash_password(pwd),uid))
                        audit(user['id'],'MODIFICAR_USUARIO','users',uid,email); st.success('Usuario actualizado.'); st.rerun()
                    except Exception as e: st.error(f'No fue posible modificar: {e}')
        elif search:
            st.info('Ajuste la búsqueda para seleccionar un usuario a modificar.')
    with tabs[2]:
        deletable={k:v for k,v in choices.items() if k!=user['id']}
        if deletable:
            uid=st.selectbox('Usuario a eliminar',list(deletable),format_func=lambda x:deletable[x],key='user_delete_select')
            st.warning('Si el usuario tiene historial de validaciones o cambios, se conservará para auditoría y quedará inactivo.')
            if st.button('Eliminar usuario',type='primary'):
                hist=query('SELECT (SELECT COUNT(*) FROM changes WHERE requested_by=?)+(SELECT COUNT(*) FROM approvals WHERE user_id=?)+(SELECT COUNT(*) FROM audit_log WHERE user_id=?) n',(uid,uid,uid))[0]['n']
                execute('DELETE FROM assignments WHERE user_id=?',(uid,))
                if hist:
                    execute('UPDATE users SET active=0 WHERE id=?',(uid,)); detail='Usuario inactivado por conservar historial'
                else:
                    execute('DELETE FROM notifications WHERE user_id=?',(uid,)); execute('DELETE FROM users WHERE id=?',(uid,)); detail='Usuario eliminado'
                audit(user['id'],'ELIMINAR_USUARIO','users',uid,detail); st.success(detail+'.'); st.rerun()
        elif search:
            st.info('Ajuste la búsqueda para seleccionar un usuario a eliminar.')

def _next_activity_code():
    rows=query("SELECT code FROM activities WHERE code LIKE 'ACT-%'")
    nums=[]
    for r in rows:
        try: nums.append(int(str(r['code']).split('-')[1]))
        except Exception: pass
    return f"ACT-{(max(nums) if nums else 0)+1:03d}"

def activities_admin(user):
    st.title('Mantenedor de actividades')
    rows=query("""SELECT a.id,a.code,a.name,a.group_id,g.name grupo,a.description,a.observations,a.active FROM activities a JOIN activity_groups g ON g.id=a.group_id ORDER BY a.id""")
    search=st.text_input('Buscar actividad',placeholder='Escriba ID, actividad, grupo, descripción u observación...',key='activities_search')
    filtered=[r for r in rows if _contains_text([r['code'],r['name'],r['grupo'],r['description'],r['observations']],search)]
    st.caption(f'{len(filtered)} actividad(es) encontrada(s)')
    if filtered:
        df=pd.DataFrame([dict(r) for r in filtered])[['code','name','grupo','description','observations','active']].rename(columns={'code':'ID','name':'Actividad','grupo':'Grupo actividad','description':'Descripción','observations':'Observaciones','active':'Activa'})
        st.dataframe(df,hide_index=True,use_container_width=True)
    else:
        st.info('No hay actividades que coincidan con la búsqueda.')
    groups=query('SELECT id,name FROM activity_groups ORDER BY id'); gl={r['id']:r['name'] for r in groups}
    semesters=[r['semester'] for r in query('SELECT DISTINCT semester FROM periods ORDER BY year,semester')]
    tabs=st.tabs(['Agregar','Modificar','Eliminar'])
    with tabs[0]:
        with st.form('activity_add'):
            code=st.text_input('ID actividad',value=_next_activity_code(),disabled=True)
            name=st.text_input('Actividad'); gid=st.selectbox('Grupo actividad',list(gl),format_func=lambda x:gl[x]); desc=st.text_area('Descripción'); obs=st.text_area('Observaciones')
            sems=st.multiselect('Periodos/semestres en que estará disponible',semesters)
            if st.form_submit_button('Agregar actividad',type='primary'):
                if not name.strip(): st.error('Ingrese el nombre de la actividad.')
                else:
                    aid=execute('INSERT INTO activities(code,name,group_id,description,observations,active) VALUES(?,?,?,?,?,1)',(code,name.strip(),gid,desc.strip(),obs.strip()))
                    for sem in sems: execute('INSERT OR IGNORE INTO occurrences(activity_id,semester) VALUES(?,?)',(aid,sem))
                    audit(user['id'],'AGREGAR_ACTIVIDAD','activities',aid,code); st.success('Actividad agregada.'); st.rerun()
    al={r['id']:f"{r['code']} · {r['name']} · {r['grupo']}" for r in filtered}
    with tabs[1]:
        if al:
            aid=st.selectbox('Actividad a modificar',list(al),format_func=lambda x:al[x],key='activity_edit_select',help='El selector también permite buscar escribiendo parte del texto.'); r=next(x for x in rows if x['id']==aid)
            current_sems=[x['semester'] for x in query('SELECT semester FROM occurrences WHERE activity_id=? ORDER BY semester',(aid,))]
            with st.form('activity_edit'):
                st.text_input('ID actividad',value=r['code'],disabled=True)
                name=st.text_input('Actividad',value=r['name']); gids=list(gl); gid=st.selectbox('Grupo actividad',gids,index=gids.index(r['group_id']),format_func=lambda x:gl[x]); desc=st.text_area('Descripción',value=r['description'] or ''); obs=st.text_area('Observaciones',value=r['observations'] or ''); active=st.checkbox('Activa',value=bool(r['active']))
                sems=st.multiselect('Periodos/semestres',semesters,default=[x for x in current_sems if x in semesters])
                if st.form_submit_button('Guardar cambios',type='primary'):
                    execute('UPDATE activities SET name=?,group_id=?,description=?,observations=?,active=? WHERE id=?',(name.strip(),gid,desc.strip(),obs.strip(),1 if active else 0,aid))
                    for sem in sems: execute('INSERT OR IGNORE INTO occurrences(activity_id,semester) VALUES(?,?)',(aid,sem))
                    for sem in set(current_sems)-set(sems):
                        oid=query('SELECT id FROM occurrences WHERE activity_id=? AND semester=?',(aid,sem))
                        if oid:
                            oid=oid[0]['id']; refs=query('SELECT (SELECT COUNT(*) FROM changes WHERE occurrence_id=?)+(SELECT COUNT(*) FROM dependencies WHERE source_occurrence_id=? OR target_occurrence_id=?) n',(oid,oid,oid))[0]['n']
                            if refs==0: execute('DELETE FROM occurrences WHERE id=?',(oid,))
                    audit(user['id'],'MODIFICAR_ACTIVIDAD','activities',aid,r['code']); st.success('Actividad actualizada.'); st.rerun()
        elif search:
            st.info('Ajuste la búsqueda para seleccionar una actividad a modificar.')
    with tabs[2]:
        if al:
            aid=st.selectbox('Actividad a eliminar',list(al),format_func=lambda x:al[x],key='activity_delete_select')
            st.warning('Si la actividad tiene historial publicado o cambios asociados, se conservará para trazabilidad y quedará inactiva.')
            if st.button('Eliminar actividad',type='primary'):
                refs=query('SELECT (SELECT COUNT(*) FROM version_occurrences WHERE activity_id=?)+(SELECT COUNT(*) FROM changes c JOIN occurrences o ON o.id=c.occurrence_id WHERE o.activity_id=?) n',(aid,aid))[0]['n']
                execute('DELETE FROM assignments WHERE activity_id=?',(aid,))
                if refs:
                    execute('UPDATE activities SET active=0 WHERE id=?',(aid,)); detail='Actividad inactivada por conservar historial'
                else:
                    oids=[x['id'] for x in query('SELECT id FROM occurrences WHERE activity_id=?',(aid,))]
                    for oid in oids: execute('DELETE FROM dependencies WHERE source_occurrence_id=? OR target_occurrence_id=?',(oid,oid))
                    execute('DELETE FROM occurrence_comments WHERE occurrence_id IN (SELECT id FROM occurrences WHERE activity_id=?)',(aid,))
                    execute('DELETE FROM occurrences WHERE activity_id=?',(aid,)); execute('DELETE FROM activities WHERE id=?',(aid,)); detail='Actividad eliminada'
                audit(user['id'],'ELIMINAR_ACTIVIDAD','activities',aid,detail); st.success(detail+'.'); st.rerun()
        elif search:
            st.info('Ajuste la búsqueda para seleccionar una actividad a eliminar.')

def periods_admin(user):
    st.title('Mantenedor de periodos')
    rows=query('SELECT * FROM periods ORDER BY year,semester')
    if rows:
        pdf=pd.DataFrame([dict(r) for r in rows]).rename(columns={'id':'ID','year':'Año','semester':'Semestre','start_date':'Fecha inicio','end_date':'Fecha fin'})
        pdf['Fecha inicio']=pdf['Fecha inicio'].map(d); pdf['Fecha fin']=pdf['Fecha fin'].map(d)
        st.dataframe(_style_calendar_dates(pdf,['Fecha inicio','Fecha fin']),hide_index=True,use_container_width=True,
                     column_config={'Fecha inicio':st.column_config.DateColumn(format='DD/MM/YYYY'),'Fecha fin':st.column_config.DateColumn(format='DD/MM/YYYY')})
    tabs=st.tabs(['Agregar','Modificar','Eliminar'])
    with tabs[0]:
        with st.form('period_add'):
            c1,c2,c3,c4=st.columns(4); y=c1.number_input('Año',min_value=2027,max_value=2100,value=2028); sem=c2.text_input('Semestre',value='2028-1'); sd=c3.date_input('Fecha inicio'); ed=c4.date_input('Fecha fin')
            if st.form_submit_button('Agregar periodo',type='primary'):
                if ed<sd: st.error('La fecha fin no puede ser anterior al inicio.')
                else:
                    try: pid=execute('INSERT INTO periods(year,semester,start_date,end_date) VALUES(?,?,?,?)',(int(y),sem.strip(),s(sd),s(ed))); audit(user['id'],'AGREGAR_PERIODO','periods',pid,sem); st.rerun()
                    except Exception as e: st.error(f'No fue posible agregar: {e}')
    pl={r['id']:f"{r['semester']} · {r['start_date']} a {r['end_date']}" for r in rows}
    with tabs[1]:
        if pl:
            pid=st.selectbox('Periodo a modificar',list(pl),format_func=lambda x:pl[x],key='period_edit_select'); r=next(x for x in rows if x['id']==pid)
            with st.form('period_edit'):
                c1,c2,c3,c4=st.columns(4); y=c1.number_input('Año',min_value=2027,max_value=2100,value=int(r['year']),key='py'); sem=c2.text_input('Semestre',value=r['semester'],key='ps'); sd=c3.date_input('Fecha inicio',value=d(r['start_date']),key='psd'); ed=c4.date_input('Fecha fin',value=d(r['end_date']),key='ped')
                if st.form_submit_button('Guardar cambios',type='primary'):
                    if ed<sd: st.error('La fecha fin no puede ser anterior al inicio.')
                    else:
                        old=r['semester']; execute('UPDATE periods SET year=?,semester=?,start_date=?,end_date=? WHERE id=?',(int(y),sem.strip(),s(sd),s(ed),pid))
                        if old!=sem.strip(): execute('UPDATE occurrences SET semester=? WHERE semester=?',(sem.strip(),old))
                        audit(user['id'],'MODIFICAR_PERIODO','periods',pid,sem); st.rerun()
    with tabs[2]:
        if pl:
            pid=st.selectbox('Periodo a eliminar',list(pl),format_func=lambda x:pl[x],key='period_delete_select'); r=next(x for x in rows if x['id']==pid)
            st.caption('Eliminar el periodo no elimina las actividades históricas ya publicadas.')
            if st.button('Eliminar periodo',type='primary'):
                execute('DELETE FROM periods WHERE id=?',(pid,)); audit(user['id'],'ELIMINAR_PERIODO','periods',pid,r['semester']); st.rerun()

def _dep_rule_type(row):
    rt=row['rule_type'] if 'rule_type' in row.keys() else None
    return rt or ('antes' if row['operator']=='<=' else 'después')


def _dep_storage(rule_type, days):
    days=max(0,int(days))
    if rule_type=='antes': return '<=',-days
    if rule_type=='igual': return '>=',0
    return '>=',days


def _dep_description(src_id, src_field, tgt_id, tgt_field, rule_type, days, occ_by_id):
    src=occ_by_id[src_id]; tgt=occ_by_id[tgt_id]
    sv='comienza' if src_field=='start_date' else 'termina'
    tv='comienza' if tgt_field=='start_date' else 'termina'
    n=max(0,int(days)); unit='día' if n==1 else 'días'
    if rule_type=='igual':
        return f"{tgt['code']} {tv} el mismo día que {src['code']} {sv}"
    if n==0:
        return f"{tgt['code']} {tv} {rule_type} de {src['code']} {sv}"
    return f"{tgt['code']} {tv} {n} {unit} {rule_type} de {src['code']} {sv}"


def dependencies_admin(user):
    st.title('Mantenedor de dependencias')
    rows=query("""SELECT d.id,d.source_occurrence_id,d.source_field,d.operator,d.offset_days,d.rule_type,d.target_occurrence_id,d.target_field,d.description,d.active,sa.code||' · '||so.semester origen,ta.code||' · '||too.semester destino
                  FROM dependencies d JOIN occurrences so ON so.id=d.source_occurrence_id JOIN activities sa ON sa.id=so.activity_id JOIN occurrences too ON too.id=d.target_occurrence_id JOIN activities ta ON ta.id=too.activity_id ORDER BY d.id""")
    if rows:
        table=[]
        for r in rows:
            table.append({'ID':r['id'],'Origen':r['origen'],'Campo origen':FIELD_LABEL[r['source_field']], 'Regla':_dep_rule_type(r).capitalize(), 'Días':abs(int(r['offset_days'])) if _dep_rule_type(r)!='igual' else 0,'Destino':r['destino'],'Campo destino':FIELD_LABEL[r['target_field']],'Descripción':r['description'],'Activa':bool(r['active'])})
        st.dataframe(pd.DataFrame(table),hide_index=True,use_container_width=True)
    occ=query("""SELECT o.id,a.code,a.name,o.semester FROM occurrences o JOIN activities a ON a.id=o.activity_id WHERE a.active=1 ORDER BY o.semester,a.id""")
    labels={r['id']:f"{r['code']} · {r['name']} · {r['semester']}" for r in occ}; ids=list(labels); occ_by_id={r['id']:r for r in occ}
    tabs=st.tabs(['Agregar','Modificar','Eliminar'])
    with tabs[0]:
        if ids:
            st.subheader('Nueva dependencia')
            src=st.selectbox('Actividad origen',ids,format_func=lambda x:labels[x],key='dep_add_src')
            c1,c2,c3=st.columns(3)
            sf=c1.selectbox('Campo origen',['end_date','start_date'],format_func=lambda x:FIELD_LABEL[x],key='dep_add_sf')
            rule_type=c2.selectbox('Regla',['antes','después','igual'],format_func=lambda x:x.capitalize(),key='dep_add_rule')
            days=c3.number_input('Días',min_value=0,value=1 if rule_type!='igual' else 0,step=1,disabled=(rule_type=='igual'),key='dep_add_days')
            tgt=st.selectbox('Actividad destino',ids,format_func=lambda x:labels[x],key='dep_add_tgt')
            tf=st.selectbox('Campo destino',['start_date','end_date'],format_func=lambda x:FIELD_LABEL[x],key='dep_add_tf')
            desc=_dep_description(src,sf,tgt,tf,rule_type,days,occ_by_id)
            st.text_input('Descripción generada',value=desc,disabled=True,key='dep_add_desc')
            st.caption('La descripción es generada automáticamente para comprobar la relación antes de guardarla.')
            if st.button('Agregar dependencia',type='primary',key='dep_add_btn'):
                if src==tgt:
                    st.error('Origen y destino deben ser actividades distintas.')
                else:
                    op,off=_dep_storage(rule_type,days)
                    did=execute('INSERT INTO dependencies(source_occurrence_id,source_field,target_occurrence_id,target_field,operator,offset_days,description,rule_type) VALUES(?,?,?,?,?,?,?,?)',(src,sf,tgt,tf,op,off,desc,rule_type))
                    audit(user['id'],'AGREGAR_DEPENDENCIA','dependencies',did,desc); st.success('Dependencia agregada.'); st.rerun()
    dl={r['id']:f"#{r['id']} · {r['origen']} → {r['destino']}" for r in rows}
    with tabs[1]:
        if dl:
            did=st.selectbox('Dependencia a modificar',list(dl),format_func=lambda x:dl[x],key='dep_edit_select'); r=next(x for x in rows if x['id']==did)
            rt=_dep_rule_type(r); current_days=0 if rt=='igual' else abs(int(r['offset_days']))
            src=st.selectbox('Actividad origen',ids,index=ids.index(r['source_occurrence_id']),format_func=lambda x:labels[x],key=f'dep_edit_src_{did}')
            c1,c2,c3=st.columns(3)
            sfv=['end_date','start_date']; sf=c1.selectbox('Campo origen',sfv,index=sfv.index(r['source_field']),format_func=lambda x:FIELD_LABEL[x],key=f'dep_edit_sf_{did}')
            rule_opts=['antes','después','igual']; rule_type=c2.selectbox('Regla',rule_opts,index=rule_opts.index(rt),format_func=lambda x:x.capitalize(),key=f'dep_edit_rule_{did}')
            days=c3.number_input('Días',min_value=0,value=current_days if rule_type!='igual' else 0,step=1,disabled=(rule_type=='igual'),key=f'dep_edit_days_{did}')
            tgt=st.selectbox('Actividad destino',ids,index=ids.index(r['target_occurrence_id']),format_func=lambda x:labels[x],key=f'dep_edit_tgt_{did}')
            tfv=['start_date','end_date']; tf=st.selectbox('Campo destino',tfv,index=tfv.index(r['target_field']),format_func=lambda x:FIELD_LABEL[x],key=f'dep_edit_tf_{did}')
            desc=_dep_description(src,sf,tgt,tf,rule_type,days,occ_by_id)
            st.text_input('Descripción generada',value=desc,disabled=True,key=f'dep_edit_desc_{did}')
            active=st.checkbox('Activa',value=bool(r['active']),key=f'dep_edit_active_{did}')
            if st.button('Guardar cambios',type='primary',key=f'dep_edit_save_{did}'):
                if src==tgt:
                    st.error('Origen y destino deben ser actividades distintas.')
                else:
                    op,off=_dep_storage(rule_type,days)
                    execute('UPDATE dependencies SET source_occurrence_id=?,source_field=?,target_occurrence_id=?,target_field=?,operator=?,offset_days=?,description=?,rule_type=?,active=? WHERE id=?',(src,sf,tgt,tf,op,off,desc,rule_type,1 if active else 0,did))
                    audit(user['id'],'MODIFICAR_DEPENDENCIA','dependencies',did,desc); st.success('Dependencia actualizada.'); st.rerun()
    with tabs[2]:
        if dl:
            did=st.selectbox('Dependencia a eliminar',list(dl),format_func=lambda x:dl[x],key='dep_delete_select')
            if st.button('Eliminar dependencia',type='primary'):
                execute('DELETE FROM dependencies WHERE id=?',(did,)); audit(user['id'],'ELIMINAR_DEPENDENCIA','dependencies',did,''); st.rerun()
    st.caption('Ejemplo: ACT-017 comienza 2 días después de ACT-016. Las reglas se evalúan automáticamente al modificar una fecha.')

def assignments_admin(user):
    st.title('Asignación de responsables por actividad')
    rows=query("""SELECT x.id,u.first_name||' '||u.last_name líder,u.email,a.code,a.name,g.name grupo FROM assignments x JOIN users u ON u.id=x.user_id JOIN activities a ON a.id=x.activity_id JOIN activity_groups g ON g.id=a.group_id ORDER BY u.last_name,a.id""")
    if rows:
        st.dataframe(pd.DataFrame([dict(r) for r in rows]).rename(columns={'id':'ID','líder':'Líder','email':'Correo','code':'ID actividad','name':'Actividad','grupo':'Grupo actividad'}),hide_index=True,use_container_width=True)
    leaders=query("SELECT id,first_name,last_name,email FROM users WHERE role='Líder' AND active=1 ORDER BY last_name,first_name")
    acts=query('SELECT a.id,a.code,a.name,a.group_id,g.name grupo FROM activities a JOIN activity_groups g ON g.id=a.group_id WHERE a.active=1 ORDER BY g.id,a.id')
    groups=query('SELECT id,name FROM activity_groups ORDER BY id')
    if not leaders or not acts:
        st.info('Se requiere al menos un líder y una actividad activa.'); return
    ul={r['id']:f"{r['first_name']} {r['last_name']} · {r['email']}" for r in leaders}
    gl={r['id']:r['name'] for r in groups}
    st.subheader('Asignar o modificar actividades de un líder')
    uid=st.selectbox('Líder',list(ul),format_func=lambda x:ul[x])
    group_options=[0]+list(gl)
    gid=st.selectbox('Filtrar por grupo de actividad',group_options,format_func=lambda x:'Todos los grupos' if x==0 else gl[x],help='Seleccione un grupo para facilitar la búsqueda y asignación de actividades.')
    filtered_acts=[r for r in acts if gid==0 or r['group_id']==gid]
    al={r['id']:f"{r['code']} · {r['name']}" for r in filtered_acts}; aids=list(al)
    current_all=[r['activity_id'] for r in query('SELECT activity_id FROM assignments WHERE user_id=? ORDER BY activity_id',(uid,))]
    current_visible=[x for x in current_all if x in aids]
    selected=st.multiselect('Actividades',aids,default=current_visible,format_func=lambda x:al[x],help='Puede seleccionar varias actividades. El selector permite buscar escribiendo parte del ID o nombre.')
    if gid!=0:
        st.caption(f'Editando asignaciones solo del grupo **{gl[gid]}**. Las asignaciones de otros grupos se conservarán.')
    c1,c2=st.columns([1,1])
    if c1.button('Guardar asignaciones',type='primary',use_container_width=True):
        if gid==0:
            execute('DELETE FROM assignments WHERE user_id=?',(uid,))
        else:
            execute('DELETE FROM assignments WHERE user_id=? AND activity_id IN (SELECT id FROM activities WHERE group_id=?)',(uid,gid))
        for aid in selected: execute('INSERT OR IGNORE INTO assignments(user_id,activity_id) VALUES(?,?)',(uid,aid))
        audit(user['id'],'MODIFICAR_ASIGNACIONES','users',uid,f'{len(selected)} actividades · grupo {gl.get(gid,"Todos")}'); st.success('Asignaciones actualizadas.'); st.rerun()
    if c2.button('Quitar asignaciones visibles',use_container_width=True):
        if gid==0:
            execute('DELETE FROM assignments WHERE user_id=?',(uid,)); detail='Todas'
        else:
            execute('DELETE FROM assignments WHERE user_id=? AND activity_id IN (SELECT id FROM activities WHERE group_id=?)',(uid,gid)); detail=gl[gid]
        audit(user['id'],'ELIMINAR_ASIGNACIONES','users',uid,detail); st.success('Asignaciones eliminadas para el filtro seleccionado.'); st.rerun()

def audit_admin(user):
    st.title('Auditoría')
    rows=query('''SELECT l.id,l.created_at,u.email,l.action,l.entity,l.entity_id,l.detail FROM audit_log l LEFT JOIN users u ON u.id=l.user_id ORDER BY l.id DESC LIMIT 500''')
    st.dataframe(pd.DataFrame([dict(r) for r in rows]),hide_index=True,use_container_width=True)

if 'user' not in st.session_state: login(); st.stop()
u=st.session_state.user; page=sidebar(u)
{'Inicio':dashboard,'Calendario':calendar_page,'Mis validaciones':validations,'Notificaciones':notifications,'Versiones':versions,
 'Copiar calendario':copy_calendar_page,'Usuarios':users_admin,'Actividades':activities_admin,'Periodos':periods_admin,'Feriados':holidays_admin,'Dependencias':dependencies_admin,'Asignaciones':assignments_admin,'Auditoría':audit_admin}[page](u)
