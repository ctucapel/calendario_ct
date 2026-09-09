import os, json, smtplib
from email.message import EmailMessage
from datetime import datetime, date
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
    return delta-rule['offset_days'] if rule['operator']=='>=' else rule['offset_days']-delta

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

def sidebar(user):
    st.sidebar.markdown('## Calendario Académico')
    st.sidebar.caption(f"{user['first_name']} {user['last_name']} · {user['role']}")
    opts=['Inicio','Calendario','Mis validaciones','Notificaciones','Versiones']
    if user['role']=='Administrador': opts += ['Usuarios','Actividades','Periodos','Dependencias','Asignaciones','Auditoría']
    page=st.sidebar.radio('Navegación',opts)
    if st.sidebar.button('Cerrar sesión'): st.session_state.clear(); st.rerun()
    return page

def dashboard(user):
    st.title('Inicio')
    pend=query("SELECT COUNT(*) n FROM approvals WHERE user_id=? AND status='Pendiente'",(user['id'],))[0]['n']
    notif=query('SELECT COUNT(*) n FROM notifications WHERE user_id=? AND read_at IS NULL',(user['id'],))[0]['n']
    pub=query("SELECT version_no,published_at FROM versions WHERE status='PUBLICADA' ORDER BY year DESC,version_no DESC LIMIT 1")
    own=query('SELECT COUNT(*) n FROM assignments WHERE user_id=?',(user['id'],))[0]['n']
    c1,c2,c3,c4=st.columns(4); c1.metric('Actividades asignadas',own); c2.metric('Validaciones pendientes',pend); c3.metric('Notificaciones nuevas',notif); c4.metric('Última versión',f"V{pub[0]['version_no']}" if pub else 'Sin publicar')
    st.info('Los líderes pueden editar únicamente las fechas de las actividades asignadas. Las dependencias se revisan automáticamente y solo se activa aprobación cuando disminuye la holgura o se incumple una regla.')

def calendar_page(user):
    st.title('Calendario')
    sem=st.selectbox('Semestre',['Todos']+[r['semester'] for r in query('SELECT DISTINCT semester FROM occurrences ORDER BY semester')])
    sql='''SELECT o.id,a.id activity_id,a.code,g.name grupo,a.name,o.semester,o.start_date,o.end_date FROM occurrences o JOIN activities a ON a.id=o.activity_id JOIN activity_groups g ON g.id=a.group_id WHERE a.active=1'''; params=[]
    if sem!='Todos': sql+=' AND o.semester=?'; params.append(sem)
    rows=query(sql+' ORDER BY g.id,a.id,o.semester',params)
    df=pd.DataFrame([dict(r) for r in rows])
    if df.empty: st.warning('Sin actividades.'); return
    st.dataframe(df[['code','grupo','name','semester','start_date','end_date']].rename(columns={'code':'ID','grupo':'Grupo','name':'Actividad','semester':'Semestre','start_date':'Inicio','end_date':'Término'}),use_container_width=True,hide_index=True)
    if user['role']=='Líder':
        assigned={r['activity_id'] for r in query('SELECT activity_id FROM assignments WHERE user_id=?',(user['id'],))}
        editable=[r for r in rows if r['activity_id'] in assigned]
        st.subheader('Proponer modificación')
        if not editable: st.caption('No tiene actividades asignadas.'); return
        label={r['id']:f"{r['code']} · {r['name']} · {r['semester']}" for r in editable}
        oid=st.selectbox('Actividad',list(label),format_func=lambda x:label[x]); o=next(x for x in editable if x['id']==oid)
        c1,c2=st.columns(2); ns=c1.date_input('Nueva fecha inicio',value=d(o['start_date']) or date.today()); ne=c2.date_input('Nueva fecha término',value=d(o['end_date']) or d(o['start_date']) or date.today())
        impacts=dependency_impacts(oid,ns,ne)
        if impacts:
            st.warning(f'Este cambio reduce o incumple {len(impacts)} dependencia(s) y requerirá aprobación.')
            st.dataframe(pd.DataFrame(impacts)[['code','name','semester','old_slack','new_slack']].rename(columns={'code':'ID','name':'Actividad impactada','semester':'Semestre','old_slack':'Holgura anterior','new_slack':'Nueva holgura'}),hide_index=True,use_container_width=True)
        else: st.success('No se detecta reducción de holgura. El cambio irá directamente a borrador para aprobación del administrador.')
        if st.button('Enviar cambio',type='primary'):
            cid,_=propose_change(user,oid,ns,ne); st.success(f'Cambio #{cid} registrado.'); st.rerun()

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

def users_admin(user):
    st.title('Mantenedor de usuarios')
    st.dataframe(pd.DataFrame([dict(r) for r in query('SELECT id,first_name,last_name,email,unit,role,active FROM users ORDER BY id')]),hide_index=True,use_container_width=True)
    with st.form('newuser'):
        c1,c2,c3=st.columns(3); fn=c1.text_input('Nombre'); ln=c2.text_input('Apellido'); email=c3.text_input('Correo')
        c4,c5,c6=st.columns(3); unit=c4.text_input('Unidad'); role=c5.selectbox('Rol',['Líder','Visualizador','Administrador']); pwd=c6.text_input('Contraseña inicial',type='password')
        if st.form_submit_button('Crear usuario',type='primary'):
            try: execute('INSERT INTO users(first_name,last_name,email,unit,role,password_hash) VALUES(?,?,?,?,?,?)',(fn,ln,email,unit,role,hash_password(pwd))); st.success('Usuario creado.'); st.rerun()
            except Exception as e: st.error(f'No fue posible crear: {e}')

def activities_admin(user):
    st.title('Mantenedor de actividades')
    rows=query('''SELECT a.id,a.code,a.name,g.name grupo,a.description,a.observations,a.active FROM activities a JOIN activity_groups g ON g.id=a.group_id ORDER BY a.id''')
    st.dataframe(pd.DataFrame([dict(r) for r in rows]),hide_index=True,use_container_width=True)
    st.caption('Las fechas se administran en Calendario; aquí se mantiene la definición estructural de la actividad.')

def periods_admin(user):
    st.title('Mantenedor de periodos')
    st.dataframe(pd.DataFrame([dict(r) for r in query('SELECT * FROM periods ORDER BY year,semester')]),hide_index=True,use_container_width=True)
    with st.form('period'):
        c1,c2,c3,c4=st.columns(4); y=c1.number_input('Año',min_value=2027,max_value=2100,value=2028); sem=c2.text_input('Semestre',value='2028-1'); sd=c3.date_input('Fecha inicio'); ed=c4.date_input('Fecha fin')
        if st.form_submit_button('Agregar periodo'): execute('INSERT OR REPLACE INTO periods(year,semester,start_date,end_date) VALUES(?,?,?,?)',(int(y),sem,s(sd),s(ed))); st.rerun()

def dependencies_admin(user):
    st.title('Mantenedor de dependencias')
    rows=query('''SELECT d.id,sa.code||' · '||so.semester origen,d.source_field,d.operator,d.offset_days,ta.code||' · '||too.semester destino,d.target_field,d.description,d.active
                  FROM dependencies d JOIN occurrences so ON so.id=d.source_occurrence_id JOIN activities sa ON sa.id=so.activity_id JOIN occurrences too ON too.id=d.target_occurrence_id JOIN activities ta ON ta.id=too.activity_id ORDER BY d.id''')
    if rows: st.dataframe(pd.DataFrame([dict(r) for r in rows]),hide_index=True,use_container_width=True)
    occ=query('''SELECT o.id,a.code,a.name,o.semester FROM occurrences o JOIN activities a ON a.id=o.activity_id ORDER BY o.semester,a.id'''); labels={r['id']:f"{r['code']} · {r['name']} · {r['semester']}" for r in occ}
    with st.form('dep'):
        src=st.selectbox('Actividad origen',list(labels),format_func=lambda x:labels[x]); sf=st.selectbox('Campo origen',['end_date','start_date'],format_func=lambda x:FIELD_LABEL[x])
        op=st.selectbox('Regla',['>=','<=']); off=st.number_input('Días de separación (+/-)',value=1,step=1)
        tgt=st.selectbox('Actividad destino',list(labels),format_func=lambda x:labels[x]); tf=st.selectbox('Campo destino',['start_date','end_date'],format_func=lambda x:FIELD_LABEL[x]); desc=st.text_input('Descripción',placeholder='Ej.: B comienza 3 días después del término de A')
        if st.form_submit_button('Crear dependencia',type='primary'):
            execute('INSERT INTO dependencies(source_occurrence_id,source_field,target_occurrence_id,target_field,operator,offset_days,description) VALUES(?,?,?,?,?,?,?)',(src,sf,tgt,tf,op,int(off),desc)); st.rerun()
    st.caption('Interpretación: Campo destino >=/<= Campo origen + días. Ej.: B inicia 3 días después de A → origen=A término, destino=B inicio, >=, 3 días.')

def assignments_admin(user):
    st.title('Asignación de responsables por actividad')
    rows=query('''SELECT x.id,u.first_name||' '||u.last_name líder,u.email,a.code,a.name,g.name grupo FROM assignments x JOIN users u ON u.id=x.user_id JOIN activities a ON a.id=x.activity_id JOIN activity_groups g ON g.id=a.group_id ORDER BY u.last_name,a.id''')
    if rows: st.dataframe(pd.DataFrame([dict(r) for r in rows]),hide_index=True,use_container_width=True)
    leaders=query("SELECT id,first_name,last_name,email FROM users WHERE role='Líder' AND active=1 ORDER BY last_name"); acts=query('SELECT id,code,name FROM activities WHERE active=1 ORDER BY id')
    if leaders and acts:
        ul={r['id']:f"{r['first_name']} {r['last_name']} · {r['email']}" for r in leaders}; al={r['id']:f"{r['code']} · {r['name']}" for r in acts}
        with st.form('assign'):
            uid=st.selectbox('Líder',list(ul),format_func=lambda x:ul[x]); aid=st.selectbox('Actividad',list(al),format_func=lambda x:al[x])
            if st.form_submit_button('Asignar'): execute('INSERT OR IGNORE INTO assignments(user_id,activity_id) VALUES(?,?)',(uid,aid)); st.rerun()

def audit_admin(user):
    st.title('Auditoría')
    rows=query('''SELECT l.id,l.created_at,u.email,l.action,l.entity,l.entity_id,l.detail FROM audit_log l LEFT JOIN users u ON u.id=l.user_id ORDER BY l.id DESC LIMIT 500''')
    st.dataframe(pd.DataFrame([dict(r) for r in rows]),hide_index=True,use_container_width=True)

if 'user' not in st.session_state: login(); st.stop()
u=st.session_state.user; page=sidebar(u)
{'Inicio':dashboard,'Calendario':calendar_page,'Mis validaciones':validations,'Notificaciones':notifications,'Versiones':versions,
 'Usuarios':users_admin,'Actividades':activities_admin,'Periodos':periods_admin,'Dependencias':dependencies_admin,'Asignaciones':assignments_admin,'Auditoría':audit_admin}[page](u)
