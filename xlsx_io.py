"""Minimal, dependency-free XLSX single-worksheet importer/exporter.

Supports text, numeric/boolean/formula literals, bold/italic/underline,
font color, solid fill and alignment. Does not calculate formulas, import
pictures, VBA, merged-cell geometry, or external workbook references.
"""
import io
import json
import re
import zipfile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

N='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
P='http://schemas.openxmlformats.org/package/2006/relationships'


def letter(col):
    col+=1
    out=''
    while col:
        col,rem=divmod(col-1,26)
        out=chr(65+rem)+out
    return out


def coordinates(ref):
    m=re.fullmatch(r'([A-Z]{1,3})([1-9][0-9]{0,6})',ref.upper())
    if not m:raise ValueError('Некорректный адрес XLSX: '+ref)
    col=0
    for ch in m.group(1):col=col*26+ord(ch)-64
    row=int(m.group(2))-1
    if row>=100000 or col>16384:raise ValueError('XLSX выходит за лимит таблицы ИТСО')
    return row,col-1


def rgb(elem):
    if elem is None:return None
    color=elem.get('rgb','')
    return '#'+color[-6:].upper() if re.fullmatch(r'[a-fA-F0-9]{8}',color) else None


def parse_styles(z):
    if 'xl/styles.xml' not in z.namelist():return [{}]
    root=ET.fromstring(z.read('xl/styles.xml'))
    fonts=[];fills=[];alignment=[]
    fonts_xml=root.find('{%s}fonts'%N)
    if fonts_xml is not None:
        for f in fonts_xml:
            st={}
            for flag in ('b','i','u'):
                if f.find('{%s}%s'%(N,flag)) is not None:st[{'b':'bold','i':'italic','u':'underline'}[flag]]=True
            c=rgb(f.find('{%s}color'%N))
            if c:st['color']=c
            fonts.append(st)
    fills_xml=root.find('{%s}fills'%N)
    if fills_xml is not None:
        for fill in fills_xml:
            pf=fill.find('{%s}patternFill'%N)
            color=rgb(pf.find('{%s}fgColor'%N)) if pf is not None and pf.get('patternType')=='solid' else None
            fills.append(color)
    styles=[]
    xfs=root.find('{%s}cellXfs'%N)
    if xfs is not None:
        for xf in xfs:
            fid=int(xf.get('fontId',0));fill=int(xf.get('fillId',0))
            st=dict(fonts[fid]) if fid<len(fonts) else {}
            if fill<len(fills) and fills[fill]:st['bg']=fills[fill]
            align=xf.find('{%s}alignment'%N)
            if align is not None and align.get('horizontal') in ('left','right','center'):
                st['align']=align.get('horizontal')
            styles.append(st)
    return styles or [{}]


def import_xlsx(blob):
    if len(blob)>16*1024*1024:raise ValueError('XLSX больше 16 МиБ')
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names=set(z.namelist())
        if any(n.startswith('/') or '..' in n.split('/') for n in names):raise ValueError('Недопустимые пути в XLSX')
        if any(i.file_size>40*1024*1024 for i in z.infolist()):raise ValueError('Слишком большой XML в XLSX')
        if 'xl/workbook.xml' not in names:raise ValueError('Отсутствует книга XLSX')
        styles=parse_styles(z)
        strings=[]
        if 'xl/sharedStrings.xml' in names:
            shared=ET.fromstring(z.read('xl/sharedStrings.xml'))
            strings=[''.join((t.text or '') for t in si.iter('{%s}t'%N)) for si in shared]
        sheetname=next((name for name in sorted(names) if re.fullmatch(r'xl/worksheets/sheet\d+\.xml',name)),None)
        if sheetname is None:raise ValueError('Нет листов XLSX')
        sheet=ET.fromstring(z.read(sheetname))
        cells=[]
        for e in sheet.iter('{%s}c'%N):
            row,col=coordinates(e.get('r',''))
            raw=e.find('{%s}v'%N)
            formula=e.find('{%s}f'%N)
            inline=e.find('{%s}is'%N)
            typ=e.get('t','')
            if formula is not None:
                value='='+(formula.text or '')
            elif typ=='inlineStr' and inline is not None:
                value=''.join((t.text or '') for t in inline.iter('{%s}t'%N))
            elif raw is None:
                value=''
            elif typ=='s':
                index=int(raw.text or '0')
                if index>=len(strings):raise ValueError('Некорректная sharedStrings ссылка')
                value=strings[index]
            elif typ=='b':
                value='TRUE' if raw.text=='1' else 'FALSE'
            else:value=raw.text or ''
            index=int(e.get('s','0'))
            if not 0<=index<len(styles):raise ValueError('Некорректный индекс стиля')
            if len(value)>20000:raise ValueError('Слишком длинная ячейка')
            if value or styles[index]:cells.append((row,col,value,styles[index]))
        if len(cells)>350000:raise ValueError('Слишком много заполненных ячеек')
        return cells


def export_xlsx(cells,title='Лист1'):
    # One compact stylesheet entry for each distinct local style.
    combos=[{}];combo_ids={'{}':0}
    for c in cells:
        st=c.get('style') or {}
        key=json.dumps(st,sort_keys=True,ensure_ascii=False)
        if key not in combo_ids:combo_ids[key]=len(combos);combos.append(st)
    fonts=[{}];font_ids={'{}':0};fills=[None];fill_ids={None:0}
    style_info=[]
    for st in combos:
        font={k:st[k] for k in ('bold','italic','underline','color') if k in st}
        fkey=json.dumps(font,sort_keys=True)
        if fkey not in font_ids:font_ids[fkey]=len(fonts);fonts.append(font)
        fill=st.get('bg')
        if fill not in fill_ids:fill_ids[fill]=len(fills)+1;fills.append(fill)
        style_info.append((font_ids[fkey],fill_ids[fill],st.get('align','')))
    parts=['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>','<worksheet xmlns="'+N+'"><sheetData>']
    prev=-1
    for c in sorted(cells,key=lambda e:(e['row_no'],e['col_no'])):
        row=c['row_no'];col=c['col_no']
        if row!=prev:
            if prev!=-1:parts.append('</row>')
            parts.append('<row r="{}">'.format(row+1));prev=row
        coord=letter(col)+str(row+1)
        st=json.dumps(c.get('style') or {},sort_keys=True,ensure_ascii=False)
        val=str(c.get('value',''))
        # Numeric values need no automatic conversion; preserving leading 0 is vital for IP/IDs.
        parts.append('<c r="{}" s="{}" t="inlineStr"><is><t xml:space="preserve">{}</t></is></c>'.format(
            coord,combo_ids[st],escape(val)))
    if prev!=-1:parts.append('</row>')
    parts.append('</sheetData></worksheet>')
    fonts_xml=[]
    for font in fonts:
        f='<font><sz val="11"/><name val="Arial"/>'
        if font.get('bold'):f+='<b/>'
        if font.get('italic'):f+='<i/>'
        if font.get('underline'):f+='<u/>'
        if re.fullmatch(r'#[A-Fa-f0-9]{6}',font.get('color','')):f+='<color rgb="FF'+font['color'][1:]+'"/>'
        fonts_xml.append(f+'</font>')
    fills_xml=['<fill><patternFill patternType="none"/></fill>','<fill><patternFill patternType="gray125"/></fill>']
    for fill in fills[1:]:
        if re.fullmatch(r'#[A-Fa-f0-9]{6}',fill or ''):fills_xml.append('<fill><patternFill patternType="solid"><fgColor rgb="FF'+fill[1:]+'"/><bgColor indexed="64"/></patternFill></fill>')
        else:fills_xml.append('<fill><patternFill patternType="none"/></fill>')
    xf_xml=[]
    for fid,fill,align in style_info:
        attr=' numFmtId="0" fontId="{}" fillId="{}" borderId="0" xfId="0" applyFont="1" applyFill="1"'.format(fid,fill)
        if align in ('left','center','right'):xf_xml.append('<xf'+attr+' applyAlignment="1"><alignment horizontal="'+align+'"/></xf>')
        else:xf_xml.append('<xf'+attr+'/>')
    styles='<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="'+N+'"><fonts count="'+str(len(fonts))+'">'+''.join(fonts_xml)+'</fonts><fills count="'+str(len(fills_xml))+'">'+''.join(fills_xml)+'</fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="'+str(len(xf_xml))+'">'+''.join(xf_xml)+'</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','''<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>''')
        z.writestr('_rels/.rels','''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>''')
        z.writestr('xl/workbook.xml','<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="'+N+'" xmlns:r="'+R+'"><sheets><sheet name='+quoteattr(re.sub(r'[\[\]:*?/\\]', '_', title)[:31] or 'Лист1')+' sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>''')
        z.writestr('xl/worksheets/sheet1.xml',''.join(parts))
        z.writestr('xl/styles.xml',styles)
    return out.getvalue()
