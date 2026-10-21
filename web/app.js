/* ITSO browser client: vanilla JavaScript, no external libraries, no emoji icons. */
(function () {
'use strict';
var $ = function(id){return document.getElementById(id);};
var state = {user:null,csrf:'',view:'search',sheets:[],maps:[],currentSheet:0,currentMap:0,
  search:{q:'',page:1,section:'all',scope:'',exact:false,results:null,dirty:false},
  selectedCell:null,selectedObject:null,connectSource:null,mapData:null,zoom:1,
  lastChatId:0,timer:null,searchTimer:null};
function escapeHtml(x){return String(x==null?'':x).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function toast(message){var t=$('toast');t.textContent=message;t.hidden=false;clearTimeout(state.timer);state.timer=setTimeout(function(){t.hidden=true;},5000);}
function status(s){$('status').textContent=s;}
function query(path,opts){opts=opts||{};var init={method:opts.method||'GET',credentials:'same-origin',headers:{'X-ITSO-CSRF':state.csrf}};
  if(opts.body!==undefined){init.headers['Content-Type']='application/json';init.body=JSON.stringify(opts.body);}
  if(opts.file){init.headers['X-Filename']=encodeURIComponent(opts.file.name);init.body=opts.file;init.method='POST';}
  if(opts.raw){init.body=opts.raw;init.headers['Content-Type']='application/octet-stream';init.headers['X-Upload-Offset']=String(opts.offset);}
  return fetch(path,init).then(function(r){return r.json().catch(function(){return {error:'Неверный ответ сервера'};}).then(function(d){
    if(!r.ok){var error=Error(d.error||('HTTP '+r.status));error.httpStatus=r.status;throw error;}return d;});}).catch(function(e){
    if(/Войдите в систему/.test(e.message)){showLogin();}throw e;
  });}
function safe(fn){return Promise.resolve().then(fn).catch(function(e){toast(e.message||String(e));status('Ошибка: '+(e.message||e));});}
function showLogin(){$('login').hidden=false;$('shell').hidden=true;state.user=null;state.csrf='';state.mapData=null;state.sheetData=null;state.cellLookup={};state.search={q:'',page:1,section:'all',scope:'',exact:false,results:null,dirty:false};}
function login(){$('login-error').textContent='';safe(function(){return query('/api/login',{method:'POST',body:{name:$('login-name').value,password:$('login-password').value}}).then(function(d){
  state.user=d.user;state.csrf=d.csrf;initShell();});});}
function initShell(){$('login').hidden=true;$('shell').hidden=false;$('login-password').value='';$('identity').textContent=state.user.name+' · '+({admin:'Администратор',editor:'Редактор',observer:'Наблюдатель'}[state.user.role]);
  navigate('search');setInterval(function(){if(!state.user)return;query('/api/me').then(function(d){var left=d.expires-Math.floor(Date.now()/1000);if(left<300&&left>0&&left>=280)toast('Сеанс скоро завершится. Сохраните изменения.');}).catch(function(){});},60000);
}
function writable(){return state.user&&(state.user.role==='admin'||state.user.role==='editor');}
function navigate(section){state.view=section;state.selectedObject=null;document.querySelectorAll('[data-view]').forEach(function(b){b.classList.toggle('nav-active',b.dataset.view===section);});
  $('panel-heading').textContent={search:'Поиск',tables:'Таблицы',maps:'Карты',files:'Обменник',chat:'Чат'}[section];
  safe(function(){return ({search:renderSearch,tables:renderTables,maps:renderMaps,files:renderFiles,chat:renderChat})[section]();});}
function renderSearch(){var s=state.search;$('panel').innerHTML='<div class="toolbar"><input id="q" type="search" placeholder="Название, IP, номер или текст" style="flex:1;min-width:200px"><button id="go">Найти</button><label><input id="exact" type="checkbox"> Точное совпадение</label></div>'+
  '<div class="toolbar"><label>Раздел <select id="section"><option value="all">Все</option><option value="maps">Карты</option><option value="tables">Таблицы</option><option value="files">Обменник</option><option value="chat">Чат</option></select></label><label>Место <input id="scope" placeholder="Папка или ID" size="17"></label></div><div id="search-content" class="box search-results"></div><div id="search-paging" class="toolbar"></div>';
  $('q').value=s.q;$('exact').checked=s.exact;$('section').value=s.section;$('scope').value=s.scope;
  $('q').addEventListener('input',function(){s.q=this.value;s.page=1;clearTimeout(state.searchTimer);if(s.q.trim().length>=3)state.searchTimer=setTimeout(function(){safe(runSearch);},500);});
  $('q').addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();safe(runSearch);}});
  $('go').onclick=function(){safe(runSearch);};$('exact').onchange=function(){s.exact=this.checked;safe(runSearch);};
  $('section').onchange=function(){s.section=this.value;s.page=1;safe(runSearch);};$('scope').onchange=function(){s.scope=this.value;s.page=1;safe(runSearch);};
  paintSearch();}
function runSearch(){var s=state.search;s.q=$('q').value;s.scope=$('scope').value;s.exact=$('exact').checked;s.section=$('section').value;
  if(!s.q.trim()){s.results=null;paintSearch();return Promise.resolve();}
  var stamp=s.q+'|'+s.page+'|'+s.section+'|'+s.scope+'|'+s.exact;
  return query('/api/search?q='+encodeURIComponent(s.q)+'&page='+s.page+'&section='+s.section+'&scope='+encodeURIComponent(s.scope)+'&exact='+(s.exact?'1':'0')).then(function(data){
    if(state.view!=='search'||stamp!==s.q+'|'+s.page+'|'+s.section+'|'+s.scope+'|'+s.exact)return;
    s.results=data;s.dirty=false;paintSearch();status('Найдено: '+data.total);});}
function paintSearch(){if(state.view!=='search')return;var s=state.search;var box=$('search-content'),paging=$('search-paging');if(!box)return;
 if(!s.results){box.textContent='Введите запрос для поиска по базе.';paging.textContent='';return;}
 var d=s.results;if(!d.items.length){box.textContent='Ничего не найдено';paging.textContent='';return;}
 var rows=d.items.map(function(r,i){return '<tr data-result="'+i+'"><td>'+escapeHtml(r.label)+'</td><td>'+escapeHtml({maps:'Карты',tables:'Таблицы',files:'Обменник',chat:'Чат'}[r.section])+'</td><td>'+escapeHtml(r.location)+'</td><td>'+highlight(r.detail,s.q)+'</td></tr>';}).join('');
 box.innerHTML='<p>Найдено: '+d.total+' · Карты: '+d.counts.maps+' · Таблицы: '+d.counts.tables+' · Обменник: '+d.counts.files+' · Чат: '+d.counts.chat+'</p>'+
 '<table class="file-table"><thead><tr><th>Название</th><th>Раздел</th><th>Расположение</th><th>Совпадение</th></tr></thead><tbody>'+rows+'</tbody></table>';
 box.querySelectorAll('[data-result]').forEach(function(r){r.ondblclick=function(){var item=d.items[Number(this.dataset.result)];openResult(item);};});
 paging.innerHTML='<button id="p-prev" '+(s.page===1?'disabled':'')+'>Назад</button><span>Страница '+s.page+' из '+Math.max(1,Math.ceil(d.total/50))+'</span><button id="p-next" '+(s.page>=Math.ceil(d.total/50)?'disabled':'')+'>Вперёд</button>';
 $('p-prev').onclick=function(){s.page--;safe(runSearch);};$('p-next').onclick=function(){s.page++;safe(runSearch);};}
function highlight(text,q){var val=String(text||'').slice(0,200);var terms=q.trim().split(/\s+/).filter(Boolean);var out='',start=0;
  var rx=new RegExp('('+terms.map(function(x){return x.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');}).join('|')+')','ig');
  if(!terms.length)return escapeHtml(val);
  val.replace(rx,function(m,_,i){out+=escapeHtml(val.slice(start,i))+'<mark>'+escapeHtml(m)+'</mark>';start=i+m.length;return m;});return out+escapeHtml(val.slice(start));}
function openResult(item){if(item.section==='maps'){state.currentMap=item.ref;navigate('maps');}
  else if(item.section==='tables'){state.currentSheet=item.ref;navigate('tables');}
  else if(item.section==='files'){navigate('files');}
  else if(item.section==='chat'){navigate('chat');}}
function loadSheetList(){return query('/api/sheets').then(function(x){state.sheets=x;if(!state.currentSheet&&x.length)state.currentSheet=x[0].id;});}
function renderTables(){return loadSheetList().then(function(){var panel=$('panel');panel.innerHTML='<div class="toolbar"><strong>Excel-подобные таблицы</strong><span class="spacer"></span>'+(writable()?'<button id="new-sheet">Новая таблица</button>':'')+'</div><div class="split"><div id="sheets-list" class="box list-panel"></div><div class="flex-grow" style="display:flex;flex-direction:column;gap:7px;min-width:0"><div id="sheet-toolbar" class="toolbar"></div><div id="sheet-grid" class="edit-grid-outer"></div></div></div>';
 if(writable())$('new-sheet').onclick=function(){var title=prompt('Название новой таблицы:');if(title)safe(function(){return query('/api/sheets',{method:'POST',body:{title:title}}).then(function(d){state.currentSheet=d.id;renderTables();});});};
 var list=$('sheets-list');list.innerHTML=state.sheets.length?state.sheets.map(function(x){return '<div class="list-item '+(x.id===state.currentSheet?'selected':'')+'" data-id="'+x.id+'">'+escapeHtml((x.folder?x.folder+' / ':'')+x.title)+'</div>';}).join(''):'Пока нет таблиц.';
 list.querySelectorAll('[data-id]').forEach(function(e){e.onclick=function(){state.currentSheet=Number(this.dataset.id);safe(renderTables);};});
 if(state.currentSheet)return loadSheet(state.currentSheet);});}
function cellKey(r,c){return r+':'+c;}
function loadSheet(id){return query('/api/sheets/'+id).then(function(d){if(state.view!=='tables')return;state.sheetData=d;var lookup={};d.cells.forEach(function(c){lookup[cellKey(c.row_no,c.col_no)]=c;});state.cellLookup=lookup;
 $('sheet-toolbar').innerHTML='<span>'+escapeHtml(d.sheet.title)+'</span>'+(writable()?'<button id="bold-cell" class="small">Жирный</button><button id="fill-cell" class="small">Заливка</button>':'')+'<span class="muted">Двойной щелчок — изменить ячейку</span>';
 var html='<table class="edit-grid"><thead><tr><th class="row-heading"></th>';
 for(var col=0;col<12;col++)html+='<th>'+String.fromCharCode(65+col)+'</th>';html+='</tr></thead><tbody>';
 for(var row=0;row<65;row++){html+='<tr><th class="row-heading">'+(row+1)+'</th>';for(var col=0;col<12;col++){
 var c=lookup[cellKey(row,col)];var sty=c?c.style:{};
 html+='<td data-row="'+row+'" data-col="'+col+'" style="'+cellStyle(sty)+'">'+escapeHtml(c?c.value:'')+'</td>';}
 html+='</tr>';}html+='</tbody></table>';
 $('sheet-grid').innerHTML=html;$('sheet-grid').querySelectorAll('td').forEach(function(td){td.onclick=function(){selectCell(this);};td.ondblclick=function(){selectCell(this);editCell();};});
 if(writable()){$('bold-cell').onclick=function(){changeStyle('bold');};$('fill-cell').onclick=function(){changeStyle('fill');};}
 });}
function cellStyle(sty){return 'background:'+(/^(#[0-9a-fA-F]{6})$/.test(sty.bg||'')?sty.bg:'#fff')+';color:'+(/^(#[0-9a-fA-F]{6})$/.test(sty.color||'')?sty.color:'#111')+';font-weight:'+(sty.bold?'bold':'normal');}
function selectCell(td){document.querySelectorAll('.active-cell').forEach(function(x){x.classList.remove('active-cell');});td.classList.add('active-cell');state.selectedCell={row:Number(td.dataset.row),col:Number(td.dataset.col)};}
function applyCell(value,style){var p=state.selectedCell;if(!p)return;var old=state.cellLookup[cellKey(p.row,p.col)]||{revision:0,style:{},value:''};
 return query('/api/sheets/'+state.currentSheet+'/cell',{method:'PUT',body:{row:p.row,col:p.col,value:value,style:style,revision:old.revision}}).then(function(){return loadSheet(state.currentSheet);});}
function editCell(){if(!writable())return;var p=state.selectedCell;if(!p)return;var old=state.cellLookup[cellKey(p.row,p.col)]||{value:'',style:{}};
 var v=prompt('Значение ячейки '+String.fromCharCode(65+p.col)+(p.row+1)+':',old.value);if(v!==null)safe(function(){return applyCell(v,old.style);});}
function changeStyle(key){var p=state.selectedCell;if(!p){toast('Сначала выделите ячейку');return;}var old=state.cellLookup[cellKey(p.row,p.col)]||{value:'',style:{}};
 var style=Object.assign({},old.style);if(key==='bold')style.bold=!style.bold;else{var v=prompt('Цвет заливки (#RRGGBB):',style.bg||'#FFFFCC');if(!/^#[0-9a-fA-F]{6}$/.test(v||''))return;style.bg=v;}
 safe(function(){return applyCell(old.value,style);});}
function loadMapList(){return query('/api/maps').then(function(x){state.maps=x;if(!state.currentMap&&x.length)state.currentMap=x[0].id;});}
function renderMaps(){return loadMapList().then(function(){var panel=$('panel');panel.innerHTML='<div class="toolbar"><strong>Карты ИТСО</strong><span class="spacer"></span>'+(writable()?'<button id="new-map">Новая карта</button>':'')+'</div><div class="split"><div id="maps-list" class="box list-panel"></div><div class="flex-grow" style="display:flex;flex-direction:column;gap:7px;min-width:0"><div class="toolbar" id="map-tools"></div><div class="split" style="min-height:0"><div class="map-area"><svg id="map-svg" viewBox="0 0 800 480" aria-label="Редактор карты"></svg><div class="map-hint">Перетаскивайте объекты мышью · Правая кнопка — добавить</div></div><aside id="map-properties" class="properties" hidden></aside></div></div></div>';
 if(writable())$('new-map').onclick=function(){var title=prompt('Название новой карты:');if(title)safe(function(){return query('/api/maps',{method:'POST',body:{title:title}}).then(function(d){state.currentMap=d.id;renderMaps();});});};
 var list=$('maps-list');list.innerHTML=state.maps.length?state.maps.map(function(x){return '<div class="list-item '+(x.id===state.currentMap?'selected':'')+'" data-id="'+x.id+'">'+escapeHtml((x.folder?x.folder+' / ':'')+x.title)+'</div>';}).join(''):'Пока нет карт.';
 list.querySelectorAll('[data-id]').forEach(function(el){el.onclick=function(){state.currentMap=Number(el.dataset.id);safe(renderMaps);};});
 if(state.currentMap)return loadMap(state.currentMap);});}
function loadMap(id){return query('/api/maps/'+id).then(function(d){if(state.view!=='maps')return;state.mapData=d;state.connectSource=null;
 $('map-tools').innerHTML='<span>'+escapeHtml(d.map.title)+'</span>'+(writable()?'<button id="add-object">Добавить объект</button><button id="connect-object">Соединить</button><button id="delete-object">Удалить</button>':'')+'<button id="ping-object">PING</button><button id="map-zoom-plus">+</button><button id="map-zoom-minus">−</button><button id="map-zoom-all">100%</button>';
 if(writable()){$('add-object').onclick=function(){safe(function(){return addObject(150,110);});};$('connect-object').onclick=function(){state.connectSource='choose';toast('Выберите первый и второй объекты');};$('delete-object').onclick=function(){safe(deleteSelected);};}
 $('ping-object').onclick=function(){safe(pingSelected);};$('map-zoom-plus').onclick=function(){state.zoom=Math.min(3,state.zoom*1.25);paintMap();};$('map-zoom-minus').onclick=function(){state.zoom=Math.max(.4,state.zoom/1.25);paintMap();};$('map-zoom-all').onclick=function(){state.zoom=1;paintMap();};
 var svg=$('map-svg');svg.addEventListener('contextmenu',function(e){e.preventDefault();if(writable()){var p=svgPoint(e);safe(function(){return addObject(p.x,p.y);});}});
 paintMap();});}
function svgPoint(e){var r=$('map-svg').getBoundingClientRect();return {x:(e.clientX-r.left)*800/r.width/state.zoom,y:(e.clientY-r.top)*480/r.height/state.zoom};}
function paintMap(){if(!state.mapData)return;var svg=$('map-svg'),data=state.mapData;svg.setAttribute('viewBox','0 0 '+(800/state.zoom)+' '+(480/state.zoom));
 var lookup={};data.objects.forEach(function(o){lookup[o.id]=o;});var out='<rect width="4000" height="4000" fill="#fcfcfc" />';
 for(var x=0;x<800/state.zoom;x+=30)out+='<line x1="'+x+'" y1="0" x2="'+x+'" y2="'+(480/state.zoom)+'" stroke="#f0f0f0"/>';
 for(var y=0;y<480/state.zoom;y+=30)out+='<line x1="0" y1="'+y+'" x2="'+(800/state.zoom)+'" y2="'+y+'" stroke="#f0f0f0"/>';
 data.links.forEach(function(l){var a=lookup[l.from_id],b=lookup[l.to_id];if(a&&b)out+='<line x1="'+(a.x+55)+'" y1="'+(a.y+19)+'" x2="'+(b.x+55)+'" y2="'+(b.y+19)+'" stroke="#316c9e" stroke-width="2" />';});
 data.objects.forEach(function(o){var title=String(o.properties['Название']||'Объект').slice(0,24);
 out+='<g class="object '+(state.selectedObject===o.id?'selected':'')+'" data-oid="'+escapeHtml(o.id)+'" transform="translate('+Number(o.x)+' '+Number(o.y)+')"><rect width="110" height="38" fill="#dbe8f7" stroke="#436c95" stroke-width="2" /><text x="55" y="23" text-anchor="middle" fill="#1f4062">'+escapeHtml(title)+'</text></g>';});
 svg.innerHTML=out;
 svg.querySelectorAll('.object').forEach(function(el){el.addEventListener('pointerdown',objectDown);el.addEventListener('click',objectClick);el.addEventListener('dblclick',function(){selectObject(this.dataset.oid,true);});});}
var moving=null;
function objectDown(e){if(!writable()||state.connectSource)return;var o=state.mapData.objects.find(function(x){return x.id===e.currentTarget.dataset.oid;});if(!o)return;
 moving={id:o.id,start:svgPoint(e),x:o.x,y:o.y};e.currentTarget.setPointerCapture(e.pointerId);
 e.currentTarget.addEventListener('pointermove',objectMove);e.currentTarget.addEventListener('pointerup',objectUp,{once:true});}
function objectMove(e){if(!moving)return;var pt=svgPoint(e),g=e.currentTarget;g.setAttribute('transform','translate('+(moving.x+pt.x-moving.start.x)+' '+(moving.y+pt.y-moving.start.y)+')');}
function objectUp(e){e.currentTarget.removeEventListener('pointermove',objectMove);if(!moving)return;var mv=moving;moving=null;var p=svgPoint(e),dx=p.x-mv.start.x,dy=p.y-mv.start.y;
 if(Math.abs(dx)+Math.abs(dy)<3)return;var obj=state.mapData.objects.find(function(o){return o.id===mv.id;});if(!obj)return;
 safe(function(){return query('/api/maps/'+state.currentMap+'/objects',{method:'PUT',body:{revision:obj.revision,object:{id:obj.id,x:mv.x+dx,y:mv.y+dy,properties:obj.properties,appearance:obj.appearance}}}).then(function(){return loadMap(state.currentMap);});});}
function objectClick(e){var id=e.currentTarget.dataset.oid;
 if(state.connectSource){if(state.connectSource==='choose'){state.connectSource=id;toast('Выберите второй объект');}else{var src=state.connectSource;state.connectSource=null;safe(function(){return query('/api/maps/'+state.currentMap+'/links',{method:'POST',body:{from:src,to:id}}).then(function(){return loadMap(state.currentMap);});});}return;}
 selectObject(id,false);}
function selectObject(id,edit){state.selectedObject=id;paintMap();var obj=state.mapData.objects.find(function(o){return o.id===id;});if(!obj)return;var side=$('map-properties');side.hidden=false;
 side.innerHTML='<strong>Свойства объекта</strong><span class="muted">Поля задаются вручную</span><label>Название<input id="obj-name" value="'+escapeHtml(obj.properties['Название']||'')+'" '+(!writable()?'disabled':'')+'></label><label>Дополнительные характеристики (JSON)<textarea id="obj-props" rows="7" '+(!writable()?'disabled':'')+'></textarea></label>'+(writable()?'<button id="save-object">Сохранить свойства</button>':'');
 var extra=Object.assign({},obj.properties);delete extra['Название'];$('obj-props').value=JSON.stringify(extra,null,2);
 if(writable())$('save-object').onclick=function(){safe(function(){var props=JSON.parse($('obj-props').value||'{}');if(!props||Array.isArray(props)||typeof props!=='object')throw Error('Ожидается объект JSON');props['Название']=$('obj-name').value;
 return query('/api/maps/'+state.currentMap+'/objects',{method:'PUT',body:{revision:obj.revision,object:{id:obj.id,x:obj.x,y:obj.y,properties:props,appearance:obj.appearance}}}).then(function(){return loadMap(state.currentMap);});});};
 if(edit&&writable())$('obj-name').focus();}
function addObject(x,y){var name=prompt('Название нового объекта:');if(!name)return Promise.resolve();return query('/api/maps/'+state.currentMap+'/objects',{method:'PUT',body:{revision:0,object:{x:x,y:y,properties:{'Название':name},appearance:{}}}}).then(function(){return loadMap(state.currentMap);});}
function deleteSelected(){var id=state.selectedObject;if(!id){toast('Выберите объект');return Promise.resolve();}var connected=state.mapData.links.filter(function(l){return l.from_id===id||l.to_id===id;}).length;
 if(connected&& !confirm('Объект имеет '+connected+' соединений. В текущей версии удаление свободных концов ещё не реализовано. Удалить объект ВМЕСТЕ с линиями?'))return Promise.resolve();
 if(!connected&&!confirm('Окончательно удалить объект?'))return Promise.resolve();
 return query('/api/maps/'+state.currentMap+'/objects/'+id,{method:'DELETE',body:{delete_links:true}}).then(function(){state.selectedObject=null;$('map-properties').hidden=true;return loadMap(state.currentMap);});}
function pingSelected(){var id=state.selectedObject;if(!id){toast('Выберите объект');return Promise.resolve();}var obj=state.mapData.objects.find(function(o){return o.id===id;});var ip=prompt('IPv4 для однократной проверки PING:',obj.properties.IP||'');
 if(!ip)return Promise.resolve();status('Проверка ICMP...');return query('/api/ping',{method:'POST',body:{ip:ip}}).then(function(d){toast(d.reachable?'Получен ответ ICMP':'Нет ответа ICMP');});}
// Files are sent in small independently confirmed chunks. The upload identifier is
// stored per user/file so reselecting the same local file can resume after a LAN outage.
function uploadFile(file){
 var partSize=1024*1024;
 var key='itso-upload:'+state.user.id+':'+file.name+':'+file.size+':'+file.lastModified;
 function cached(){try{return localStorage.getItem(key);}catch(e){return null;}}
 function remember(value){try{if(value)localStorage.setItem(key,value);else localStorage.removeItem(key);}catch(e){}}
 function fresh(){return query('/api/uploads',{method:'POST',body:{name:file.name,size:file.size}}).then(function(d){remember(d.id);return d;});}
 function stateForId(id){if(!id)return fresh();return query('/api/uploads/'+id).catch(function(e){if(e.httpStatus===404){remember(null);return fresh();}throw e;});}
 function next(d){
   if(d.received>=d.size){return query('/api/uploads/'+d.id+'/finish',{method:'POST',body:{}}).then(function(done){remember(null);return done;});}
   var end=Math.min(d.size,d.received+partSize);
   status('Загрузка '+file.name+': '+Math.floor(100*d.received/Math.max(1,d.size))+'%');
   return query('/api/uploads/'+d.id,{method:'PUT',raw:file.slice(d.received,end),offset:d.received}).then(next);
 }
 return stateForId(cached()).then(function(d){
   if(d.complete){remember(null);return d;}
   return next(d);
 });
}
function renderFiles(){return query('/api/files').then(function(files){if(state.view!=='files')return;
 $('panel').innerHTML='<div class="toolbar"><strong>Общий список файлов</strong><span class="spacer"></span><input id="upload" type="file" multiple></div><div class="box flex-grow" id="filebox"></div>';
 var h='<table class="file-table"><thead><tr><th>Имя файла</th><th>Размер</th><th>Загрузил</th><th>Действия</th></tr></thead><tbody>';
 files.forEach(function(f){h+='<tr><td>'+escapeHtml(f.name)+'</td><td>'+human(f.byte_length)+'</td><td>'+escapeHtml(f.owner)+'</td><td><a href="/api/files/'+escapeHtml(f.id)+'">Скачать</a>'+(state.user.role==='admin'?' <button class="small" data-delete="'+f.id+'">Удалить</button>':'')+'</td></tr>';});
 $('filebox').innerHTML=h+'</tbody></table>';
 $('upload').onchange=function(){var items=Array.from(this.files);var work=Promise.resolve();items.forEach(function(file){work=work.then(function(){status('Загрузка: '+file.name);return uploadFile(file);});});safe(function(){return work.then(function(){status('Файлы загружены');return renderFiles();});});};
 $('filebox').querySelectorAll('[data-delete]').forEach(function(b){b.onclick=function(){var id=this.dataset.delete;if(!confirm('Окончательно удалить файл?'))return;safe(function(){return query('/api/files/'+id,{method:'DELETE',body:{}}).then(renderFiles);});};});
 });}
function human(n){var units=['Б','КБ','МБ','ГБ','ТБ'];var i=0;while(n>=1024&&i<units.length-1){n/=1024;i++;}return (i?n.toFixed(1):n)+' '+units[i];}
function renderChat(){return query('/api/chat').then(function(messages){if(state.view!=='chat')return;
 $('panel').innerHTML='<div class="toolbar"><strong>Общий рабочий чат</strong><span class="spacer"></span><button id="refresh-chat">Обновить</button><button id="older-chat">Ранние сообщения</button></div><div class="box flex-grow" id="chat-messages"></div><div class="chat-entry"><textarea id="chat-draft" placeholder="Введите сообщение..."></textarea><div class="actions"><button id="send-chat">Отправить</button></div></div>';
 showChat(messages);$('refresh-chat').onclick=function(){safe(renderChat);};$('older-chat').onclick=function(){safe(function(){var first=Number($('chat-messages').dataset.first);if(!first)return;return query('/api/chat?before='+first).then(function(older){showChat(older.concat(messages));});});};
 $('send-chat').onclick=function(){var body=$('chat-draft').value;if(!body.trim())return;safe(function(){return query('/api/chat',{method:'POST',body:{body:body}}).then(renderChat);});};
 $('chat-draft').addEventListener('keydown',function(e){if(e.ctrlKey&&e.key==='Enter')$('send-chat').click();});});}
function showChat(messages){var box=$('chat-messages');box.dataset.first=messages.length?messages[0].id:0;box.innerHTML=messages.map(function(m){var edit=m.user_id===state.user.id&&Date.now()/1000-m.created<900;
 return '<div class="msg"><span class="by">'+escapeHtml(m.username)+'</span> <span class="time">'+new Date(m.created*1000).toLocaleString('ru-RU')+(m.edited?' · изменено':'')+'</span>'+
 (edit?'<button data-edit="'+m.id+'">Править</button>':'')+(state.user.role==='admin'?'<button data-delete="'+m.id+'">Удалить</button>':'')+'<div>'+escapeHtml(m.body)+'</div></div>';}).join('');box.scrollTop=box.scrollHeight;
 box.querySelectorAll('[data-edit]').forEach(function(b){b.onclick=function(){var id=Number(this.dataset.edit);var m=messages.find(function(x){return x.id===id;});var text=prompt('Изменить сообщение:',m.body);if(text!==null)safe(function(){return query('/api/chat/'+id,{method:'PUT',body:{body:text}}).then(renderChat);});};});
 box.querySelectorAll('[data-delete]').forEach(function(b){b.onclick=function(){var id=this.dataset.delete;if(confirm('Удалить сообщение?'))safe(function(){return query('/api/chat/'+id,{method:'DELETE',body:{}}).then(renderChat);});};});}
$('login-btn').onclick=login;$('login-password').onkeydown=function(e){if(e.key==='Enter')login();};
$('logout-btn').onclick=function(){safe(function(){return query('/api/logout',{method:'POST',body:{}}).then(showLogin);});};
document.querySelectorAll('[data-view]').forEach(function(b){b.onclick=function(){navigate(this.dataset.view);};});
query('/api/me').then(function(u){state.user={id:u.id,name:u.name,role:u.role};state.csrf=u.csrf;initShell();}).catch(function(){showLogin();});
})();
