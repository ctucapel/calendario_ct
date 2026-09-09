import os, io, sqlite3, calendar
from datetime import datetime, date
import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak

DUOC_YELLOW='#F1B634'; DUOC_BLACK='#1A1A1A'; DUOC_GRAY='#666666'

def _df(con, year):
    return pd.read_sql_query('''
      SELECT a.id AS ID, a.code AS Código, g.name AS Grupo, a.name AS Actividad, o.semester AS Semestre,
             o.start_date AS Inicio, o.end_date AS Término, a.observations AS Observaciones
      FROM occurrences o JOIN activities a ON a.id=o.activity_id JOIN activity_groups g ON g.id=a.group_id
      LEFT JOIN periods p ON p.semester=o.semester AND p.year=?
      ORDER BY g.id,a.id,o.semester''', con, params=(year,))

def export_all(db_path, year, version_no, out_dir='exports'):
    os.makedirs(out_dir,exist_ok=True)
    con=sqlite3.connect(db_path)
    df=_df(con,year)
    paths=[]
    # Procesos
    p1=os.path.join(out_dir,f'CA_Procesos_{year}_V{version_no}.xlsx')
    with pd.ExcelWriter(p1,engine='xlsxwriter') as w:
        df.to_excel(w,index=False,sheet_name='CA Procesos')
        _format_sheet(w,'CA Procesos',df)
    paths.append(p1)
    # Cronológico
    chrono=df.copy(); chrono['Orden']=pd.to_datetime(chrono['Inicio'],errors='coerce').fillna(pd.to_datetime(chrono['Término'],errors='coerce'))
    chrono=chrono.sort_values(['Orden','Semestre','ID']).drop(columns=['Orden'])
    p2=os.path.join(out_dir,f'CA_Cronologico_{year}_V{version_no}.xlsx')
    with pd.ExcelWriter(p2,engine='xlsxwriter') as w:
        chrono.to_excel(w,index=False,sheet_name='CA Cronológico'); _format_sheet(w,'CA Cronológico',chrono)
    paths.append(p2)
    # Gráfico - matriz mensual simplificada
    p3=os.path.join(out_dir,f'CA_Grafico_{year}_V{version_no}.xlsx')
    with pd.ExcelWriter(p3,engine='xlsxwriter') as w:
        for month in range(1,13):
            mname=f'{month:02d}-{calendar.month_name[month]}'
            first=date(year,month,1); last=date(year,month,calendar.monthrange(year,month)[1])
            rows=[]
            for _,r in df.iterrows():
                s=pd.to_datetime(r['Inicio'],errors='coerce'); e=pd.to_datetime(r['Término'],errors='coerce')
                if pd.isna(s) and pd.isna(e): continue
                sd=s.date() if not pd.isna(s) else e.date(); ed=e.date() if not pd.isna(e) else s.date()
                if sd<=last and ed>=first:
                    rows.append([r['Código'],r['Grupo'],r['Actividad'],r['Semestre'],r['Inicio'],r['Término']])
            mdf=pd.DataFrame(rows,columns=['Código','Grupo','Actividad','Semestre','Inicio','Término'])
            mdf.to_excel(w,index=False,sheet_name=mname[:31]); _format_sheet(w,mname[:31],mdf)
    paths.append(p3)
    # PDF consolidado
    pdf=os.path.join(out_dir,f'CA_Calendario_{year}_V{version_no}.pdf')
    _pdf(pdf,year,version_no,chrono,df)
    paths.append(pdf)
    con.close(); return paths

def _format_sheet(writer,sheet_name,df):
    wb=writer.book; ws=writer.sheets[sheet_name]
    hdr=wb.add_format({'bold':True,'bg_color':DUOC_YELLOW,'font_color':DUOC_BLACK,'border':1,'align':'center','valign':'vcenter'})
    txt=wb.add_format({'border':1,'valign':'top','text_wrap':True})
    for col, name in enumerate(df.columns): ws.write(0,col,name,hdr)
    ws.freeze_panes(1,0); ws.autofilter(0,0,max(len(df),1),max(len(df.columns)-1,0))
    widths={'ID':7,'Código':12,'Grupo':30,'Actividad':58,'Semestre':12,'Inicio':13,'Término':13,'Observaciones':30}
    for col,name in enumerate(df.columns): ws.set_column(col,col,widths.get(name,18),txt)

def _pdf(path,year,version,chrono,df):
    doc=SimpleDocTemplate(path,pagesize=landscape(A4),leftMargin=24,rightMargin=24,topMargin=25,bottomMargin=25)
    styles=getSampleStyleSheet(); story=[]
    title=ParagraphStyle('title',parent=styles['Title'],fontName='Helvetica-Bold',fontSize=18,textColor=colors.HexColor(DUOC_BLACK))
    sub=ParagraphStyle('sub',parent=styles['Normal'],fontName='Helvetica',fontSize=9,textColor=colors.HexColor(DUOC_GRAY))
    story += [Paragraph(f'Calendario Académico {year} · V{version}',title),Paragraph('Versión publicada · formatos Procesos, Cronológico y vista mensual',sub),Spacer(1,12)]
    data=[['Sem.','Grupo','Actividad','Inicio','Término']]
    for _,r in chrono.iterrows(): data.append([r['Semestre'],r['Grupo'],Paragraph(str(r['Actividad']),styles['BodyText']),r['Inicio'] or '',r['Término'] or ''])
    t=Table(data,colWidths=[48,120,360,70,70],repeatRows=1)
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor(DUOC_YELLOW)),('TEXTCOLOR',(0,0),(-1,0),colors.HexColor(DUOC_BLACK)),('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),('GRID',(0,0),(-1,-1),0.3,colors.lightgrey),('VALIGN',(0,0),(-1,-1),'TOP'),('FONTSIZE',(0,0),(-1,-1),7),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#F7F7F7')])]))
    story.append(t); story.append(PageBreak())
    story += [Paragraph('Vista gráfica mensual',title),Spacer(1,8)]
    for month in range(1,13):
        first=date(year,month,1); last=date(year,month,calendar.monthrange(year,month)[1]); items=[]
        for _,r in df.iterrows():
            s=pd.to_datetime(r['Inicio'],errors='coerce'); e=pd.to_datetime(r['Término'],errors='coerce')
            if pd.isna(s) and pd.isna(e): continue
            sd=s.date() if not pd.isna(s) else e.date(); ed=e.date() if not pd.isna(e) else s.date()
            if sd<=last and ed>=first: items.append(f"{r['Código']} · {r['Actividad']} ({r['Inicio'] or '—'} a {r['Término'] or '—'})")
        story.append(Paragraph(f'<b>{calendar.month_name[month]}</b>',styles['Heading3']))
        story.append(Paragraph('<br/>'.join(items) if items else 'Sin actividades registradas.',sub)); story.append(Spacer(1,6))
    doc.build(story)
