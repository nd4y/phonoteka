import { useEffect, useRef, useState } from 'react';
import { api, messageFor } from './api';

type Plan = {id:string;state:string;created_at:string;error:string|null;job_id:string|null;multi_file_atomic:false;
  plan_sha256:string;members:Array<{track_id:string;ready_path:string;work_revision:string}>;
  objects:Array<{path:string;kind:string;sha256:string;size:number;action:string;owners:Record<string,unknown>}>;
  results:Record<string,unknown>;options:{cover:boolean;lrc:boolean}};
const labels:Record<string,string> = {planned:'План подготовлен',committing:'Публикуется',partial:'Часть файлов опубликована',
  complete:'Все sidecar подтверждены',interrupted:'Требуется сверка и повтор',blocked:'План заблокирован'};
const actions:Record<string,string> = {add:'Добавить',replace:'Заменить',unchanged:'Без перезаписи'};

export function SidecarPublicationPanel({ids,disabled=false,onQueued}:{ids:string[];disabled?:boolean;onQueued?:(id:string)=>void}) {
  const [cover,setCover]=useState(false),[lrc,setLrc]=useState(false),[plans,setPlans]=useState<Plan[]>([]);
  const [plan,setPlan]=useState<Plan|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[jobId,setJobId]=useState('');
  const serial=useRef(0),request=useRef<{key:string;body:string}|null>(null);
  const idsKey=ids.join(',');
  async function listing(){try{const result=await api<{items:Plan[]}>('/publication/sidecars');setPlans(result.items);}catch(err){setError(messageFor(err));}}
  useEffect(()=>{serial.current++;setPlan(null);request.current=null;setError('');setBusy(false);},[idsKey]);
  useEffect(()=>{if(!jobId)return;let alive=true;const timer=window.setInterval(()=>{void api<{state:string}>(`/jobs/${encodeURIComponent(jobId)}`).then(async status=>{
    if(!alive||['queued','running'].includes(status.state))return;
    setJobId('');if(plan){const current=await api<Plan>(`/publication/sidecars/plans/${encodeURIComponent(plan.id)}`);if(alive)setPlan(current);}if(alive)await listing();
  }).catch(err=>{if(alive){setError(messageFor(err));setJobId('');}});},1500);return()=>{alive=false;window.clearInterval(timer);};},[jobId,plan?.id]);
  async function preview(){const generation=++serial.current;setBusy(true);setError('');try{
    const value=await api<Plan>('/publication/sidecars/plans',{method:'POST',body:JSON.stringify({track_ids:ids,cover,lrc})});
    if(serial.current===generation){setPlan(value);request.current=null;await listing();}
  }catch(err){if(serial.current===generation)setError(messageFor(err));}finally{if(serial.current===generation)setBusy(false);}}
  async function publish(){if(!plan)return;setBusy(true);setError('');const generation=++serial.current;
    request.current??={key:crypto.randomUUID(),body:JSON.stringify({kind:'publish_sidecars',track_ids:plan.members.map(member=>member.track_id),params:{plan_id:plan.id}})};
    try{const result=await api<{id:string}>('/jobs',{method:'POST',headers:{'Idempotency-Key':request.current.key},body:request.current.body});
      if(serial.current===generation){setJobId(result.id);onQueued?.(result.id);request.current=null;}
    }catch(err){if(serial.current===generation)setError(messageFor(err));}finally{if(serial.current===generation)setBusy(false);}}
  return <section className="panel settings-panel"><div className="panel-heading"><h3>Дополнительные файлы Ready</h3><button className="button subtle" disabled={busy} onClick={()=>void listing()}>Сохраненные планы</button></div>
    <p className="muted small">По умолчанию выключено. Для выбранного уже опубликованного аудио можно создать cover.jpg и отдельный LRC. Встроенные теги, текст и обложка сохраняются. Каждый файл публикуется отдельно; при прерывании состав пакета виден в плане.</p>
    <div className="button-row"><label><input type="checkbox" checked={cover} disabled={busy||!!jobId} onChange={event=>setCover(event.target.checked)}/> cover.jpg из встроенной front cover</label><label><input type="checkbox" checked={lrc} disabled={busy||!!jobId} onChange={event=>setLrc(event.target.checked)}/> LRC из встроенного синхронизированного текста</label></div>
    <p className="muted small">PNG преобразуется в JPEG с белым фоном. Если у треков одной папки разные обложки или пересекаются пути, план остановится до записи. Изменение общей обложки требует выбора всех ее владельцев.</p>
    <button className="button secondary" disabled={disabled||busy||!!jobId||!ids.length||ids.length>200||!cover&&!lrc} onClick={()=>void preview()}>Подготовить план для {ids.length} треков</button>
    {!!plans.length&&<label>Сохраненный план<select disabled={busy||!!jobId} value={plan?.id||''} onChange={event=>{setPlan(plans.find(item=>item.id===event.target.value)||null);request.current=null;setError('');}}><option value="">Выберите план</option>{plans.map(item=><option key={item.id} value={item.id}>{new Date(item.created_at).toLocaleString('ru')} · {labels[item.state]||item.state} · {item.objects.length} файлов</option>)}</select></label>}
    {error&&<p role="alert" className="warning-text">{error}</p>}
    {plan&&<><p role="status">{labels[plan.state]||plan.state} · {Object.keys(plan.results).length}/{plan.objects.length}</p>{plan.error&&<p className="warning-text">Причина: {plan.error}. Повтор сначала сверит журналы и актуальные версии.</p>}
      <div className="table-scroll"><table><thead><tr><th>Путь</th><th>Действие</th><th>Владельцев</th><th>SHA-256</th><th>Состояние</th></tr></thead><tbody>{plan.objects.map(obj=><tr key={obj.path}><td className="break-word">{obj.path}</td><td>{actions[obj.action]||obj.action}</td><td>{Object.keys(obj.owners).length}</td><td><code title={obj.sha256}>{obj.sha256.slice(0,16)}…</code></td><td>{plan.results[obj.path]?'Подтвержден':'Ожидает'}</td></tr>)}</tbody></table></div>
      {plan.state!=='complete'&&<button className="button primary" disabled={disabled||busy||!!jobId} onClick={()=>void publish()}>{jobId?'Задача в обработке':Object.keys(plan.results).length?'Продолжить после сверки':'Опубликовать по этому плану'}</button>}
      <p className="muted small">Новый экспорт включает все подтвержденные sidecar вместе с аудио. Незавершенный или устаревший пакет блокирует создание такого экспорта. Готовый неизменяемый снимок сохраняет прежние версии.</p></>}
  </section>;
}
