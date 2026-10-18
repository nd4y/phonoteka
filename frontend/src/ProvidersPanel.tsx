import {useEffect,useState} from 'react';
import {api,messageFor} from './api';
type Source={id:string;configured:boolean;calls_24h:number;retry_at?:number;state:string;capabilities:string[]};
export function ProvidersPanel(){
 const [data,setData]=useState<{daily_limit:number;providers:Source[]}|null>(null),[error,setError]=useState('');
 async function load(){try{setData(await api('/providers'));setError('');}catch(e){setError(messageFor(e));}}
 useEffect(()=>{void load();},[]);
 const names:Record<string,string>={ready:'Доступен для запросов',disabled:'Выключен',key_required:'Нужен ключ',rate_limited:'Лимит частоты',provider_unavailable:'Временно недоступен',provider_unauthorized:'Доступ отклонен'};
 return <section className="panel settings-panel"><div className="panel-heading"><h2>Музыкальные источники</h2><button className="button secondary" onClick={()=>void load()}>Обновить источники</button></div><p className="muted small">Паузы сервиса и суточный бюджет соблюдаются автоматически. Дополнительные каталоги включаются в конфигурации контейнера.</p>{error&&<p role="alert">{error}</p>}{data?.providers.map(p=><div className="history-entry" key={p.id}><strong>{p.id}</strong><p className="small">{names[p.state]||p.state} · {p.calls_24h} / {data.daily_limit} запросов за 24 часа{p.retry_at?` · повтор после ${new Date(p.retry_at*1000).toLocaleString('ru-RU')}`:''}</p></div>)}</section>;
}
