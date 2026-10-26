"""Trace search and export health. Track names remain in the owner UI only."""
import json
import os
from pathlib import Path


def build():
    ds = {'type':'jaeger','uid':os.environ.get('GRAFANA_TRACES_DATASOURCE_UID','phonoteka-jaeger')}
    panels = [
        {'id':1,'type':'text','title':'Как открыть трек','gridPos':{'x':0,'y':0,'w':24,'h':3},
         'options':{'mode':'markdown','content':'В Фонотеке откройте **Обработка → задача → Измеренные этапы → Подробная трасса в Grafana**. Внутри: попытка → этап → HTTP → DNS/TCP/TLS/заголовки/тело ответа. Имена треков остаются в Фонотеке. Старые задачи не получают измерений задним числом.\n\nДля поиска ниже можно задать теги, например `{"phonoteka.item.id":"ID файла"}` или `{"phonoteka.job.id":"ID задачи"}`. Длительности параллельных этапов нельзя суммировать как общее время.'}},
        {'id':2,'type':'table','title':'Трассы обработки · нажмите Trace ID','datasource':ds,'gridPos':{'x':0,'y':3,'w':24,'h':12},
         'targets':[{'refId':'A','datasource':ds,'queryType':'search','service':'$trace_service','tags':'$trace_tags','limit':100}],
         'options':{'showHeader':True}},
    ]
    for number, name in enumerate(('exported','failed','dropped','partial'), 3):
        panels.append({'id':number,'type':'timeseries','title':{'exported':'Доставлено spans/с','failed':'Ошибки экспорта spans/с','dropped':'Потери очереди spans/с','partial':'Отклонено приёмником spans/с'}[name],
            'gridPos':{'x':((number-3)%2)*12,'y':15+((number-3)//2)*7,'w':12,'h':7},
            'datasource':{'type':'prometheus','uid':'afjapivs17a4gc'},
            'targets':[{'refId':'A','expr':'sum by (service) (rate(phonoteka_tracing_'+name+'_spans_total{app="phonoteka"}[$__rate_interval]))','legendFormat':'{{service}}'}]})
    return {'uid':'phonoteka-tracing','title':'Фонотека / Треки, этапы и HTTP-DNS','schemaVersion':41,'version':1,
        'tags':['phonoteka','tracing'],'timezone':'browser','refresh':'30s','time':{'from':'now-1h','to':'now'},
        'templating':{'list':[{'name':'trace_service','label':'Компонент','type':'custom','query':'phonoteka-worker,phonoteka-api,phonoteka-inference','current':{'text':'phonoteka-worker','value':'phonoteka-worker'}},
            {'name':'trace_tags','label':'Поиск по ID (JSON)','type':'textbox','query':'{}','current':{'text':'{}','value':'{}'}}]},
        'panels':panels,'links':[{'type':'link','title':'Открыть Фонотеку','url':'https://phonoteka.st1.int.nd4y.icu','targetBlank':True}]}


if __name__ == '__main__':
    Path(__file__).with_name('phonoteka-tracing-dashboard.json').write_text(json.dumps(build(),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
