"""Generate the importable Grafana dashboard; no credentials in this artifact."""
import json
from pathlib import Path


def build():
    panels=[]; y=0; x=0; number=0
    source={'type':'prometheus','uid':'${DS_METRICS}'}
    base='app="phonoteka",instance=~"$instance"'
    selected=base+',service=~"$service"'
    work=base+',kind=~"$kind"'
    providers=base+',provider=~"$provider"'
    def row(title):
        nonlocal y,x,number
        if x:y+=8;x=0
        number+=1;panels.append({'id':number,'type':'row','title':title,'collapsed':False,'gridPos':{'x':0,'y':y,'w':24,'h':1},'panels':[]});y+=1
    def panel(title,expr,legend='',unit='short',kind='timeseries',width=12,description='',height=8):
        nonlocal x,y,number
        if x+width>24:y+=height;x=0
        number+=1
        target={'refId':'A','expr':expr,'legendFormat':legend,'datasource':source,'range':kind=='timeseries','instant':kind!='timeseries','format':'table' if kind=='table' else 'time_series'}
        p={'id':number,'title':title,'description':description,'type':kind,'datasource':source,'gridPos':{'x':x,'y':y,'w':width,'h':height},'targets':[target],
           'fieldConfig':{'defaults':{'unit':unit,'color':{'mode':'palette-classic'},'custom':{'drawStyle':'line','lineWidth':2,'fillOpacity':8,'spanNulls':False},'thresholds':{'mode':'absolute','steps':[{'color':'green','value':None}]}},'overrides':[]},
           'options':{'legend':{'displayMode':'table','placement':'bottom','calcs':['lastNotNull','max']},'tooltip':{'mode':'multi','sort':'desc'}}}
        if kind=='stat':p['options']={'reduceOptions':{'calcs':['lastNotNull'],'values':False},'textMode':'auto','colorMode':'value','graphMode':'none','justifyMode':'auto'}
        if kind=='table':p['options']={'showHeader':True,'cellHeight':'sm'}
        if kind=='bargauge':p['options']={'orientation':'horizontal','displayMode':'gradient','reduceOptions':{'calcs':['lastNotNull'],'values':False},'showUnfilled':True}
        panels.append(p);x+=width
        if x==24:x=0;y+=height
        return p
    row('01 / Состояние сервиса')
    panel('Компоненты доступны / 3',f'sum(up{{{selected}}})',kind='stat',width=4)
    panel('Обработчик онлайн',f'max(phonoteka_worker_online{{{base}}})',kind='stat',width=4)
    panel('Записей в каталоге',f'max(phonoteka_collection_files{{{base}}})',kind='stat',width=4)
    panel('Осталось элементов',f'sum(phonoteka_queue_items_remaining{{{work}}})',kind='stat',width=4,description='Сумма незавершенных элементов активных заданий. Один файл может входить в несколько этапов.')
    panel('Успешно / мин · последние 5 мин',f'sum(rate(phonoteka_item_attempts_total{{{work},state="succeeded"}}[5m])) * 60',kind='stat',width=4)
    panel('Ошибок · выбранный интервал',f'sum(increase(phonoteka_item_attempts_total{{{work},state=~"failed|conflict|interrupted|needs_review"}}[$__range]))',kind='stat',width=4)
    row('02 / Очереди и обработка файлов')
    panel('Задания по состоянию',f'sum by (kind,state)(phonoteka_jobs{{{work}}}) > 0','{{kind}} / {{state}}',kind='table')
    panel('Осталось по этапам',f'phonoteka_queue_items_remaining{{{work}}}','{{kind}}')
    panel('Успешные попытки / мин',f'sum by (kind)(rate(phonoteka_item_attempts_total{{{work},state="succeeded"}}[$__rate_interval])) * 60','{{kind}}')
    panel('Ошибки и отложенные попытки / мин',f'sum by (kind,state)(rate(phonoteka_item_attempts_total{{{work},state!="succeeded"}}[$__rate_interval])) * 60','{{kind}} / {{state}}')
    panel('P95 времени попытки файла',f'histogram_quantile(0.95,sum by (le,kind)(rate(phonoteka_item_duration_seconds_bucket{{{work}}}[$__rate_interval])))','{{kind}}','s',description='Включает ожидание провайдера внутри попытки. Сканирование и операции группы альбома не имеют пофайлового таймера.')
    panel('Файлы в текущем состоянии · сохраненный прогресс',f'sum by (kind,state)(phonoteka_job_items{{{work}}}) > 0','{{kind}} / {{state}}',kind='table',description='Gauge из SQLite. Не применять increase/rate: повторная попытка меняет состояние элемента.')
    panel('Возраст самого старого задания',f'phonoteka_oldest_queued_age_seconds{{{work}}}','{{kind}}','s')
    panel('Ожидание повторов провайдеров',f'phonoteka_provider_retry_wait_seconds{{{work}}}','{{kind}}','s')
    panel('Задачи ждут освобождения места',f'phonoteka_storage_wait_jobs{{{work}}}','{{kind}}',description='Ожидание безопасного резервирования до появления journal/эффекта. ENOSPC после intent требует recovery и не повторяется скрытно.')
    panel('Повтор проверки места через',f'phonoteka_storage_retry_wait_seconds{{{work}}}','{{kind}}','s')
    panel('Элементы ожидают места',f'phonoteka_storage_wait_items_remaining{{{work}}}','{{kind}}')
    panel('Задачи ожидают внешнего провайдера',f'phonoteka_provider_retry_jobs{{{work}}}','{{kind}}')
    row('03 / MusicBrainz, LRCLIB, CAA, Shazam и YouTube')
    panel('Операции / мин',f'sum by (provider,outcome)(rate(phonoteka_provider_operations_total{{{providers}}}[$__rate_interval])) * 60','{{provider}} / {{outcome}}',description='Логическая операция, включает кеш; HTTP-повторы внутри операции не считаются отдельными успехами.')
    panel('P95 операции с ожиданием и повторами',f'histogram_quantile(0.95,sum by (le,provider)(rate(phonoteka_provider_duration_seconds_bucket{{{providers}}}[$__rate_interval])))','{{provider}}','s')
    panel('Остаток бюджета запросов / 24ч',f'phonoteka_provider_budget_remaining{{{providers}}}','{{provider}}',kind='bargauge')
    panel('Пауза после ограничений / ошибок',f'phonoteka_provider_cooldown_seconds{{{providers}}}','{{provider}}','s')
    panel('Зарезервировано запросов за скользящие 24ч',f'phonoteka_provider_requests_24h{{{providers}}}','{{provider}}',description='Gauge: значение уменьшается при выходе запросов из окна. Не применять increase/rate.')
    panel('Попадания в кеш / мин',f'sum by (provider)(rate(phonoteka_provider_cache_hits_total{{{providers}}}[$__rate_interval])) * 60','{{provider}}')
    panel('Размер действующего кеша',f'phonoteka_provider_cache_bytes{{{providers}}}','{{provider}}','bytes')
    panel('Записей в действующем кеше',f'phonoteka_provider_cache_entries{{{providers}}}','{{provider}}')
    panel('Запреты политики источников / мин',f'sum by (provider,operation)(rate(phonoteka_provider_policy_denials_total{{{providers}}}[$__rate_interval])) * 60','{{provider}} / {{operation}}',description='Реально отклоненные fetch/cache/embed/export/derived_analysis. Метки ограничены известными источниками и операциями; чтение отчета не увеличивает счетчик.')
    panel('Запретов политики · выбранный интервал',f'sum by (provider,operation)(increase(phonoteka_provider_policy_denials_total{{{providers}}}[$__range]))','{{provider}} / {{operation}}',kind='table')
    row('04 / Коллекция и результаты распознавания')
    panel('Форматы файлов',f'phonoteka_collection_formats{{{base}}}','{{format}}',kind='bargauge')
    panel('Состояние записей',f'phonoteka_collection_states{{{base}}}','{{state}}',kind='bargauge')
    panel('Обложки и синхронизированный текст',f'phonoteka_collection_features{{{base},feature=~"cover|lrc"}}','{{feature}}')
    panel('Ошибки входных файлов и редактируемые форматы',f'phonoteka_collection_features{{{base},feature=~"input_error|writable"}}','{{feature}}')
    panel('Результаты источников · текущая версия аудио',f'phonoteka_analysis_sources{{{providers}}}','{{provider}} / {{state}}',kind='bargauge',description='Только анализ, SHA которого соответствует текущему файлу; совпадения требуют проверки.')
    panel('YouTube · происхождение и кандидаты',f'phonoteka_youtube_links{{{base}}}','{{state}}',kind='bargauge',description='recorded: URL сохранен в локальном источнике; owner_confirmed: выбор владельца. Поиск остается кандидатом.')
    row('05 / ASR и выравнивание текста')
    panel('ASR · задания',f'phonoteka_inference_jobs{{{base}}}','{{state}}',kind='bargauge',width=8)
    panel('ASR · модель в памяти',f'phonoteka_inference_model_loaded{{{base},service="inference"}}',kind='stat',width=8,description='0 до первого запроса ASR; это не отказ сервиса.')
    panel('ASR · возраст очереди',f'phonoteka_inference_oldest_queued_age_seconds{{{base}}}',unit='s',kind='stat',width=8)
    panel('ASR · P95 длительности',f'histogram_quantile(0.95,sum by (le)(rate(phonoteka_inference_duration_seconds_bucket{{{base},service="inference"}}[$__rate_interval])))',unit='s')
    panel('ASR · выполнения / мин',f'sum by (state)(rate(phonoteka_inference_attempts_total{{{base},service="inference"}}[$__rate_interval])) * 60','{{state}}')
    panel('ASR · память model worker',f'phonoteka_inference_model_process_resident_bytes{{{base},service="inference"}}',unit='bytes',description='Резидентные веса, буферы и decoder контролируемого model worker; CPU профиль ограничен 2 ГиБ внутри контейнера 3 ГиБ.')
    panel('ASR · запуски model worker',f'increase(phonoteka_inference_model_process_starts_total{{{base},service="inference"}}[$__range])',kind='stat',description='Один resident process разделяют ASR и alignment; после отказа веса выгружаются. Повтор того же job автоматически не запускается.')
    row('06 / HTTP API')
    panel('Запросы / с',f'sum by (service,status)(rate(phonoteka_http_requests_total{{{selected}}}[$__rate_interval]))','{{service}} / {{status}}','reqps')
    panel('P95 полного ответа',f'histogram_quantile(0.95,sum by (le,service)(rate(phonoteka_http_request_duration_seconds_bucket{{{selected}}}[$__rate_interval])))','{{service}}','s',description='Включает передачу потокового аудио. /health и /metrics исключены.')
    panel('HTTP 4xx / 5xx',f'sum by (service,status)(rate(phonoteka_http_requests_total{{{selected},status=~"4..|5.."}}[$__rate_interval]))','{{service}} / {{status}}','reqps')
    panel('Текущие запросы',f'phonoteka_http_requests_in_progress{{{selected}}}','{{service}}')
    row('07 / Ресурсы процессов и достоверность мониторинга')
    panel('CPU · ядра, 100% = одно ядро',f'rate(process_cpu_seconds_total{{{selected}}}[$__rate_interval])','{{service}}','percentunit',description='Только процесс Python, без дочерних ffmpeg/yt-dlp. Нагрузка всего контейнера доступна в общем дашборде Docker.')
    panel('Резидентная память Python',f'process_resident_memory_bytes{{{selected}}}','{{service}}','bytes',description='Без дочерних процессов и page cache контейнера.')
    panel('Открытые файловые дескрипторы',f'process_open_fds{{{selected}}}','{{service}}')
    panel('Время работы процесса',f'time() - process_start_time_seconds{{{selected}}}','{{service}}','s')
    panel('Доступность endpoint',f'up{{{selected}}}','{{service}}')
    panel('SQLite · достоверность снимка',f'phonoteka_collector_up{{{base}}}','{{service}}',description='Присутствует у app и inference. 0 означает, что значения SQLite устарели; HTTP up отдельно.')
    panel('SQLite · давность успешного снимка',f'time() - phonoteka_collector_last_success_timestamp_seconds{{{base}}}','{{service}}','s')
    panel('Heartbeat обработчика · давность',f'phonoteka_worker_heartbeat_age_seconds{{{base}}}',unit='s')
    row('08 / Уведомления и надежность outbox')
    panel('Webhook · записи по состояниям',f'phonoteka_webhook_outbox{{{base}}}','{{state}}',description='Gauge из SQLite. Pending/in-flight не удаляются по TTL; delivered/dead ограничены сроком и количеством.')
    panel('Webhook · занято / емкость',f'phonoteka_webhook_queue_slots_used{{{base}}} / clamp_min(phonoteka_webhook_queue_capacity{{{base}}},1)',unit='percentunit',description='Включает зарезервированный terminal slot для каждой принятой музыкальной задачи. При заполнении новые задачи не принимаются до освобождения места.')
    panel('Webhook · зарезервировано задачами',f'phonoteka_webhook_reserved_jobs{{{base}}}',kind='stat',width=8)
    panel('Webhook · окончательные отказы',f'phonoteka_webhook_outbox{{{base},state="dead"}}',kind='stat',width=8,description='Явный retry сохраняет event ID и не запускает повторную музыкальную обработку.')
    panel('Webhook · прежний получатель',f'phonoteka_webhook_other_destination_pending{{{base}}}',kind='stat',width=8,description='После смены endpoint старые уведомления не перенаправляются новому получателю.')
    panel('Webhook · возраст недоставленного',f'phonoteka_webhook_oldest_pending_age_seconds{{{base}}}',unit='s')
    panel('Webhook · ближайшая повторная попытка',f'phonoteka_webhook_retry_wait_seconds{{{base}}}',unit='s')
    panel('Webhook · включение / пауза / recovery',f'{{__name__=~"phonoteka_webhook_delivery_enabled|phonoteka_webhook_delivery_paused|phonoteka_webhook_recovery_suspended",{base}}}','{{__name__}}',description='Интеграция условная: enabled=0 при отсутствии явной конфигурации оператора. Ни адреса, ни секреты не являются метками.')
    row('09 / Журналы stdout')
    for title,extra in [('Все события',''),('Ошибки и предупреждения',' | filter level:in(warning,error,critical)')]:
        p=panel(title,'',kind='logs',width=24,height=12,description='JSON stdout из Docker через существующий Alloy и VictoriaLogs. Логи до обновления могут иметь прежний текстовый формат.')
        p['datasource']={'type':'victoriametrics-logs-datasource','uid':'${DS_LOGS}'}
        p['targets']=[{'refId':'A','datasource':p['datasource'],'expr':'{compose_project="phonoteka"} | unpack_json | filter service:~"$service" | filter job_id:~"$job_id" | filter request_id:~"$request_id"'+extra,'queryType':'instant','editorMode':'code'}]
        p['options']={'showTime':True,'showLabels':False,'showCommonLabels':False,'wrapLogMessage':True,'prettifyLogMessage':True,'enableLogDetails':True,'sortOrder':'Descending','dedupStrategy':'none'}
    variables=[{'name':'DS_METRICS','label':'Метрики','type':'datasource','query':'prometheus','current':{'text':'VictoriaMetrics Prometheus-compatible API','value':'afjapivs17a4gc'}},
               {'name':'DS_LOGS','label':'Логи','type':'datasource','query':'victoriametrics-logs-datasource','current':{'text':'VictoriaLogs','value':'cfpyxan0zupkwf'}},
               {'name':'instance','label':'Экземпляр','type':'query','datasource':source,'query':{'query':'label_values(phonoteka_build_info{app="phonoteka"}, instance)','refId':'instance'},'refresh':1,'current':{'text':'st1','value':'st1'}}]
    for name,label,values in [('service','Компонент','app,worker,inference'),('kind','Этап','scan,audit,apply,rollback,rollback_ready,publish,analyze,recognize,transcribe,align,extract_cue,replaygain,replaygain_album,export_ready,normalize_audio,youtube,release_audit,publish_sidecars'),('provider','Источник','musicbrainz,lrclib,coverartarchive,shazam,youtube,acoustid,theaudiodb,discogs')]:
        variables.append({'name':name,'label':label,'type':'custom','query':values,'multi':True,'includeAll':True,'allValue':'.*','current':{'text':'All','value':'$__all'}})
    for name,label in [('job_id','Job ID · логи'),('request_id','Request ID · логи')]:
        variables.append({'name':name,'label':label,'type':'textbox','query':'.*','current':{'text':'.*','value':'.*'}})
    return {'uid':'phonoteka','title':'Фонотека / Коллекция, обработка и сервисы','description':'Очереди, провайдеры, YouTube, ASR, HTTP и JSON stdout. Скорости считаются по новым попыткам с момента установки метрик; сохраненный прогресс берется из SQLite. Отсутствие событий ASR/ошибок допустимо. Фильтры не запускают и не останавливают задания.',
            'schemaVersion':41,'version':1,'editable':True,'tags':['phonoteka','media','application'],'timezone':'browser','refresh':'30s','time':{'from':'now-1h','to':'now'},'templating':{'list':variables},'panels':panels,'links':[{'type':'link','title':'Открыть Фонотеку','url':'https://phonoteka.st1.int.nd4y.icu','targetBlank':True}]}


if __name__=='__main__':
    target=Path(__file__).with_name('phonoteka-dashboard.json');dashboard=build()
    target.write_text(json.dumps(dashboard,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'path':str(target),'panels':sum(p['type']!='row' for p in dashboard['panels'])}))
