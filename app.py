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
    if user['role']=='Visualizador':
        opts=['Versiones']
    elif user['role']=='Administrador':
        opts=['Inicio','Calendario','Mis validaciones','Notificaciones','Versiones','Usuarios','Actividades','Periodos','Dependencias','Asignaciones','Auditoría']
    else:
        opts=['Inicio','Calendario','Mis validaciones','Notificaciones','Versiones']
    page=st.sidebar.radio('Navegación',opts)
    if st.sidebar.button('Cerrar sesión'):
        st.session_state.clear(); st.rerun()
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
    sql="""SELECT o.id,a.id activity_id,a.code,g.name grupo,a.name,o.semester,o.start_date,o.end_date FROM occurrences o JOIN activities a ON a.id=o.activity_id JOIN activity_groups g ON g.id=a.group_id WHERE a.active=1"""; params=[]
    if sem!='Todos': sql+=' AND o.semester=?'; params.append(sem)
    rows=query(sql+' ORDER BY g.id,a.id,o.semester',params)
    df=pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        st.warning('Sin actividades.'); return
    st.dataframe(df[['code','grupo','name','semester','start_date','end_date']].rename(columns={'code':'ID','grupo':'Grupo','name':'Actividad','semester':'Semestre','start_date':'Inicio','end_date':'Término'}),use_container_width=True,hide_index=True)

    if user['role'] not in ('Líder','Administrador'):
        return
    if user['role']=='Administrador':
        editable=list(rows)
        st.caption('Como Administrador puede modificar las fechas de cualquier actividad sin necesidad de una asignación previa.')
    else:
        assigned={r['activity_id'] for r in query('SELECT activity_id FROM assignments WHERE user_id=?',(user['id'],))}
        editable=[r for r in rows if r['activity_id'] in assigned]

    st.subheader('Modificar fechas')
    if not editable:
        st.caption('No tiene actividades asignadas.'); return
    label={r['id']:f"{r['code']} · {r['name']} · {r['semester']}" for r in editable}
    oid=st.selectbox('Actividad',list(label),format_func=lambda x:label[x]); o=next(x for x in editable if x['id']==oid)
    c1,c2=st.columns(2)
    ns=c1.date_input('Nueva fecha inicio',value=d(o['start_date']) or date.today())
    ne=c2.date_input('Nueva fecha término',value=d(o['end_date']) or d(o['start_date']) or date.today())
    if ne < ns:
        st.error('La fecha de término no puede ser anterior a la fecha de inicio.'); return
    impacts=dependency_impacts(oid,ns,ne)
    if impacts:
        st.warning(f'Este cambio reduce o incumple {len(impacts)} dependencia(s) y requerirá aprobación de los líderes impactados.')
        st.dataframe(pd.DataFrame(impacts)[['code','name','semester','old_slack','new_slack']].rename(columns={'code':'ID','name':'Actividad impactada','semester':'Semestre','old_slack':'Holgura anterior','new_slack':'Nueva holgura'}),hide_index=True,use_container_width=True)
    else:
        st.success('No se detecta reducción de holgura. El cambio irá a borrador para aprobación final del Administrador.')
    if st.button('Registrar modificación',type='primary'):
        if s(ns)==o['start_date'] and s(ne)==o['end_date']:
            st.info('No existen cambios respecto de las fechas actuales.')
        else:
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
    rows=query('SELECT id,first_name,last_name,email,unit,role,active FROM users ORDER BY last_name,first_name')
    if rows:
        df=pd.DataFrame([dict(r) for r in rows]).rename(columns={'id':'ID','first_name':'Nombre','last_name':'Apellido','email':'Correo','unit':'Unidad','role':'Rol','active':'Activo'})
        st.dataframe(df,hide_index=True,use_container_width=True)

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
    choices={r['id']:f"{r['first_name']} {r['last_name']} · {r['email']}" for r in rows}
    with tabs[1]:
        if choices:
            uid=st.selectbox('Usuario a modificar',list(choices),format_func=lambda x:choices[x],key='user_edit_select')
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
    if rows:
        df=pd.DataFrame([dict(r) for r in rows])[['code','name','grupo','description','observations','active']].rename(columns={'code':'ID','name':'Actividad','grupo':'Grupo actividad','description':'Descripción','observations':'Observaciones','active':'Activa'})
        st.dataframe(df,hide_index=True,use_container_width=True)
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
    al={r['id']:f"{r['code']} · {r['name']}" for r in rows}
    with tabs[1]:
        if al:
            aid=st.selectbox('Actividad a modificar',list(al),format_func=lambda x:al[x],key='activity_edit_select'); r=next(x for x in rows if x['id']==aid)
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
                    execute('DELETE FROM occurrences WHERE activity_id=?',(aid,)); execute('DELETE FROM activities WHERE id=?',(aid,)); detail='Actividad eliminada'
                audit(user['id'],'ELIMINAR_ACTIVIDAD','activities',aid,detail); st.success(detail+'.'); st.rerun()

def periods_admin(user):
    st.title('Mantenedor de periodos')
    rows=query('SELECT * FROM periods ORDER BY year,semester')
    if rows: st.dataframe(pd.DataFrame([dict(r) for r in rows]).rename(columns={'id':'ID','year':'Año','semester':'Semestre','start_date':'Fecha inicio','end_date':'Fecha fin'}),hide_index=True,use_container_width=True)
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

def dependencies_admin(user):
    st.title('Mantenedor de dependencias')
    rows=query("""SELECT d.id,d.source_occurrence_id,d.source_field,d.operator,d.offset_days,d.target_occurrence_id,d.target_field,d.description,d.active,sa.code||' · '||so.semester origen,ta.code||' · '||too.semester destino
                  FROM dependencies d JOIN occurrences so ON so.id=d.source_occurrence_id JOIN activities sa ON sa.id=so.activity_id JOIN occurrences too ON too.id=d.target_occurrence_id JOIN activities ta ON ta.id=too.activity_id ORDER BY d.id""")
    if rows:
        st.dataframe(pd.DataFrame([dict(r) for r in rows])[['id','origen','source_field','operator','offset_days','destino','target_field','description','active']].rename(columns={'id':'ID','origen':'Origen','source_field':'Campo origen','operator':'Regla','offset_days':'Días','destino':'Destino','target_field':'Campo destino','description':'Descripción','active':'Activa'}),hide_index=True,use_container_width=True)
    occ=query("""SELECT o.id,a.code,a.name,o.semester FROM occurrences o JOIN activities a ON a.id=o.activity_id WHERE a.active=1 ORDER BY o.semester,a.id"""); labels={r['id']:f"{r['code']} · {r['name']} · {r['semester']}" for r in occ}; ids=list(labels)
    tabs=st.tabs(['Agregar','Modificar','Eliminar'])
    with tabs[0]:
        if ids:
            with st.form('dep_add'):
                src=st.selectbox('Actividad origen',ids,format_func=lambda x:labels[x]); sf=st.selectbox('Campo origen',['end_date','start_date'],format_func=lambda x:FIELD_LABEL[x]); op=st.selectbox('Regla',['>=','<=']); off=st.number_input('Días de separación (+/-)',value=1,step=1)
                tgt=st.selectbox('Actividad destino',ids,format_func=lambda x:labels[x]); tf=st.selectbox('Campo destino',['start_date','end_date'],format_func=lambda x:FIELD_LABEL[x]); desc=st.text_input('Descripción',placeholder='Ej.: B comienza 3 días después del término de A')
                if st.form_submit_button('Agregar dependencia',type='primary'):
                    if src==tgt: st.error('Origen y destino deben ser actividades distintas.')
                    else: did=execute('INSERT INTO dependencies(source_occurrence_id,source_field,target_occurrence_id,target_field,operator,offset_days,description) VALUES(?,?,?,?,?,?,?)',(src,sf,tgt,tf,op,int(off),desc)); audit(user['id'],'AGREGAR_DEPENDENCIA','dependencies',did,desc); st.rerun()
    dl={r['id']:f"#{r['id']} · {r['origen']} → {r['destino']}" for r in rows}
    with tabs[1]:
        if dl:
            did=st.selectbox('Dependencia a modificar',list(dl),format_func=lambda x:dl[x],key='dep_edit_select'); r=next(x for x in rows if x['id']==did)
            with st.form('dep_edit'):
                src=st.selectbox('Actividad origen',ids,index=ids.index(r['source_occurrence_id']),format_func=lambda x:labels[x],key='des'); sfv=['end_date','start_date']; sf=st.selectbox('Campo origen',sfv,index=sfv.index(r['source_field']),format_func=lambda x:FIELD_LABEL[x],key='def'); opv=['>=','<=']; op=st.selectbox('Regla',opv,index=opv.index(r['operator']),key='deo'); off=st.number_input('Días de separación (+/-)',value=int(r['offset_days']),step=1,key='ded')
                tgt=st.selectbox('Actividad destino',ids,index=ids.index(r['target_occurrence_id']),format_func=lambda x:labels[x],key='det'); tfv=['start_date','end_date']; tf=st.selectbox('Campo destino',tfv,index=tfv.index(r['target_field']),format_func=lambda x:FIELD_LABEL[x],key='detf'); desc=st.text_input('Descripción',value=r['description'] or '',key='deds'); active=st.checkbox('Activa',value=bool(r['active']))
                if st.form_submit_button('Guardar cambios',type='primary'):
                    execute('UPDATE dependencies SET source_occurrence_id=?,source_field=?,target_occurrence_id=?,target_field=?,operator=?,offset_days=?,description=?,active=? WHERE id=?',(src,sf,tgt,tf,op,int(off),desc,1 if active else 0,did)); audit(user['id'],'MODIFICAR_DEPENDENCIA','dependencies',did,desc); st.rerun()
    with tabs[2]:
        if dl:
            did=st.selectbox('Dependencia a eliminar',list(dl),format_func=lambda x:dl[x],key='dep_delete_select')
            if st.button('Eliminar dependencia',type='primary'):
                execute('DELETE FROM dependencies WHERE id=?',(did,)); audit(user['id'],'ELIMINAR_DEPENDENCIA','dependencies',did,''); st.rerun()
    st.caption('Interpretación: Campo destino >=/<= Campo origen + días. Ej.: B inicia 3 días después de A → origen=A término, destino=B inicio, >=, 3 días.')

def assignments_admin(user):
    st.title('Asignación de responsables por actividad')
    rows=query("""SELECT x.id,u.first_name||' '||u.last_name líder,u.email,a.code,a.name,g.name grupo FROM assignments x JOIN users u ON u.id=x.user_id JOIN activities a ON a.id=x.activity_id JOIN activity_groups g ON g.id=a.group_id ORDER BY u.last_name,a.id""")
    if rows:
        st.dataframe(pd.DataFrame([dict(r) for r in rows]).rename(columns={'id':'ID','líder':'Líder','email':'Correo','code':'ID actividad','name':'Actividad','grupo':'Grupo actividad'}),hide_index=True,use_container_width=True)
    leaders=query("SELECT id,first_name,last_name,email FROM users WHERE role='Líder' AND active=1 ORDER BY last_name,first_name"); acts=query('SELECT id,code,name FROM activities WHERE active=1 ORDER BY id')
    if not leaders or not acts:
        st.info('Se requiere al menos un líder y una actividad activa.'); return
    ul={r['id']:f"{r['first_name']} {r['last_name']} · {r['email']}" for r in leaders}; al={r['id']:f"{r['code']} · {r['name']}" for r in acts}; aids=list(al)
    st.subheader('Asignar o modificar actividades de un líder')
    uid=st.selectbox('Líder',list(ul),format_func=lambda x:ul[x])
    current=[r['activity_id'] for r in query('SELECT activity_id FROM assignments WHERE user_id=? ORDER BY activity_id',(uid,))]
    selected=st.multiselect('Actividades',aids,default=[x for x in current if x in aids],format_func=lambda x:al[x],help='Puede seleccionar varias actividades en una sola operación.')
    c1,c2=st.columns([1,1])
    if c1.button('Guardar asignaciones',type='primary',use_container_width=True):
        execute('DELETE FROM assignments WHERE user_id=?',(uid,))
        for aid in selected: execute('INSERT OR IGNORE INTO assignments(user_id,activity_id) VALUES(?,?)',(uid,aid))
        audit(user['id'],'MODIFICAR_ASIGNACIONES','users',uid,f'{len(selected)} actividades'); st.success('Asignaciones actualizadas.'); st.rerun()
    if c2.button('Quitar todas las asignaciones',use_container_width=True):
        execute('DELETE FROM assignments WHERE user_id=?',(uid,)); audit(user['id'],'ELIMINAR_ASIGNACIONES','users',uid,'Todas'); st.rerun()

def audit_admin(user):
    st.title('Auditoría')
    rows=query('''SELECT l.id,l.created_at,u.email,l.action,l.entity,l.entity_id,l.detail FROM audit_log l LEFT JOIN users u ON u.id=l.user_id ORDER BY l.id DESC LIMIT 500''')
    st.dataframe(pd.DataFrame([dict(r) for r in rows]),hide_index=True,use_container_width=True)

if 'user' not in st.session_state: login(); st.stop()
u=st.session_state.user; page=sidebar(u)
{'Inicio':dashboard,'Calendario':calendar_page,'Mis validaciones':validations,'Notificaciones':notifications,'Versiones':versions,
 'Usuarios':users_admin,'Actividades':activities_admin,'Periodos':periods_admin,'Dependencias':dependencies_admin,'Asignaciones':assignments_admin,'Auditoría':audit_admin}[page](u)
