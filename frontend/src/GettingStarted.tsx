import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, ArrowRight, BookOpen, CircleHelp, FolderSearch, RefreshCw, Sparkles } from 'lucide-react';
import { api, messageFor } from './api';
import type { Job, Summary } from './types';
import type { Page } from './viewState';
import { activeGuideJob, guideJobKind, guideSteps, recommendedGuideStep } from './guideState';
import type { GuideStep } from './guideState';
import { countValue } from './CollectionOverview';
import './GettingStarted.css';

const labels: Record<GuideStep, string> = { folder: 'Папка', scan: 'Каталог', recognize: 'Распознавание', metadata: 'Данные', review: 'Проверка и запись', ready: 'Готовая коллекция' };
type Props = { summary: Summary | null; jobs: Job[]; busy: boolean; onNavigate: (page: Page) => void;
  step: GuideStep; onStep: (step: GuideStep) => void; onBack: () => void; canBack: boolean;
  onStart: (kind: string, ids?: string[], params?: Record<string, unknown>) => Promise<Job | null> };

export function GettingStarted({ summary, jobs, busy, onNavigate, onStart, step, onStep, onBack, canBack }: Props) {
  const [counts, setCounts] = useState<Record<string, number> | null>(null);
  const [loading, setLoading] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const request = useRef<AbortController | null>(null), actionLock = useRef(false), mounted = useRef(true);
  const stageTitle = useRef<HTMLHeadingElement>(null);
  const index = guideSteps.indexOf(step), active = activeGuideJob(jobs, step);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; request.current?.abort(); }; }, []);
  useEffect(() => { stageTitle.current?.focus({ preventScroll: true }); }, [step]);
  async function load() {
    if (!mounted.current) return;
    request.current?.abort(); const controller = new AbortController(); request.current = controller; setLoading(true);
    try { const report = await api<{ counts: Record<string, number> }>('/reports/collection', { signal: controller.signal });
      if (!controller.signal.aborted) { setCounts(report.counts); setError(''); }
    } catch (problem) { if (!controller.signal.aborted) setError(messageFor(problem)); }
    finally { if (request.current === controller) setLoading(false); }
  }
  const jobStates = jobs.map(job => `${job.id}:${job.state}`).join('|');
  useEffect(() => { void load(); return () => request.current?.abort(); }, [jobStates]);
  const number = (key: string) => { const value = countValue(counts?.[key]); return value === null ? '-' : value.toLocaleString('ru-RU'); };
  function change(next: GuideStep) { onStep(next); setNotice(''); }
  async function start() {
    const kind = guideJobKind(step);
    if (!kind || active || actionLock.current || busy) return;
    actionLock.current = true; setSubmitting(true); setNotice('');
    try {
      // The main queue contains only its newest page. An old paused/waiting
      // task still owns this stage, even when 50 newer jobs hide it there.
      const existing = await api<{ items: Job[] }>(`/jobs?active_only=true&kind=${encodeURIComponent(kind)}&page_size=1`);
      if (!mounted.current) return;
      if (existing.items.length) { onNavigate('jobs'); return; }
      const job = await onStart(kind, [], kind === 'scan' ? {} : { scope: 'pending' });
      if (job && mounted.current) { setNotice('Задача добавлена. Можно открыть ее ход или перейти к другим разделам.'); await load(); }
    } catch (problem) { if (mounted.current) setError(messageFor(problem)); }
    finally { actionLock.current = false; if (mounted.current) setSubmitting(false); }
  }
  const disabledReason = !summary ? 'Ожидаем состояние сервера.' :
    step === 'recognize' && !summary.capabilities.recognition ? 'Отправка аудиоотпечатков не включена. Проверьте настройки источников.' :
    step === 'metadata' && !summary.capabilities.metadata_search ? 'Поиск внешних метаданных не включен. Проверьте настройки источников.' :
    ['recognize','metadata'].includes(step) && !summary.total ? 'Сначала добавьте файлы в каталог.' :
    step === 'recognize' && counts?.recognition_pending === 0 ? 'Для доступных текущих файлов этот этап уже выполнен. Совпадения могли не найтись - их можно проверить вручную.' :
    step === 'metadata' && counts?.metadata_pending === 0 ? 'Для доступных текущих файлов запросы завершены. Перейдите к проверке результатов.' : '';
  return <div className="getting-started">
    <section className="panel guide-intro"><div className="guide-intro-icon"><Sparkles size={26}/></div><div><h2>Из папки в организованную коллекцию</h2><p>Сначала соберем каталог и найдем данные. Затем проверим изменения и сохраним готовые версии. Можно остановиться и вернуться к любому шагу.</p></div><button className="button subtle" onClick={() => onNavigate('help')}><CircleHelp size={18}/>Открыть справку</button></section>
    {counts && recommendedGuideStep(counts) !== step && <div className="notice info guide-recommendation"><div><strong>Необязательно начинать сначала</strong><p>{counts.current_proposals > 0 ? 'Уже есть сохраненные предложения. Начните с их проверки.' : 'По текущему каталогу можно перейти к следующему полезному действию. Это рекомендация, а не отметка готовности.'}</p></div><button className="button secondary" onClick={() => change(recommendedGuideStep(counts))}>Перейти: {labels[recommendedGuideStep(counts)]}<ArrowRight size={17}/></button></div>}
    <nav className="guide-steps" aria-label="Шаги организации коллекции">{guideSteps.map((item, i) => <button key={item} aria-current={item === step ? 'step' : undefined} onClick={() => change(item)}><span>{i+1}</span>{labels[item]}</button>)}</nav>
    <section className="panel guide-stage" aria-busy={loading}>
      <div className="guide-stage-header"><div><div className="eyebrow">ШАГ {index+1} ИЗ {guideSteps.length}</div><h2 ref={stageTitle} tabIndex={-1}>{labels[step]}</h2></div><button className="icon-button" aria-label="Обновить данные мастера" disabled={loading} onClick={() => void load()}><RefreshCw size={19}/></button></div>
      {error && <div className="notice error" role="alert">{error}</div>}{notice && <div className="notice success" role="status">{notice}</div>}
      {step === 'folder' && <><p className="guide-lead">Проверьте, что нужные файлы находятся в подключенной музыкальной папке.</p><div className="guide-folder"><FolderSearch size={25}/><div><strong>Папка, которую видит Фонотека</strong><code>{summary?.roots?.work || 'Путь пока недоступен'}</code></div></div><p className="muted">Это путь внутри приложения. Папка на сервере подключается при установке. Выбор нового источника из браузера пока недоступен.</p><details><summary>Как подключить или изменить папку</summary><p>В конфигурации установки свяжите свою папку с музыкальной папкой приложения. Проверьте, что файлы видны, а каталог готовой коллекции и служебные данные хранятся отдельно. Затем откройте настройки и выполните сканирование. Не переносите и не удаляйте оригиналы ради мастера.</p></details><button className="button secondary" onClick={() => onNavigate('settings')}>Проверить настройки</button>{summary?.read_only && <div className="notice warning">Каталог можно изучать, но запись сейчас недоступна. Причину и режим хранилища смотрите в настройках.</div>}</>}
      {step === 'scan' && <><p className="guide-lead">Найдем файлы и документы CUE, прочитаем имеющиеся теги. Аудио при сканировании не перезаписывается.</p><div className="guide-facts"><div><strong>{number('audio_files')}</strong><span>аудиофайлов в каталоге</span></div><div><strong>{number('cue_sheets')}</strong><span>документов CUE</span></div><div><strong>{number('audit_errors')}</strong><span>с ошибками проверки</span></div></div><p className="muted">После добавления новых файлов повторите сканирование. Для CUE откройте карточку альбома: прослушивание доступно сразу, извлечение композиций запускается отдельно.</p></>}
      {step === 'recognize' && <><p className="guide-lead">Определим записи по аудиоотпечатку - это особенно полезно для файлов с пустыми тегами и непонятными именами.</p><div className="guide-facts"><div><strong>{number('recognition_pending')}</strong><span>доступных файлов ждут этапа</span></div><div><strong>{number('fingerprint_matches')}</strong><span>текущих совпадений</span></div></div><p className="muted">Используется настроенный ShazamIO. Передается аудиоотпечаток; запросы подчиняются существующим согласиям и лимитам. Совпадение еще не подтверждает релиз или версию записи. Повторяем только незавершенный этап.</p></>}
      {step === 'metadata' && <><p className="guide-lead">Найдем варианты тегов, обложек и текста в настроенных музыкальных каталогах.</p><div className="guide-facts"><div><strong>{number('metadata_pending')}</strong><span>доступных файлов ждут поиска</span></div><div><strong>{number('with_candidates')}</strong><span>файлов с найденными вариантами</span></div></div><p className="muted">MusicBrainz, LRCLIB и Cover Art Archive готовят варианты. Они не записываются в аудио автоматически. Отсутствие совпадения - результат поиска, а не ошибка файла.</p><details><summary>Если текст есть, но без таймкодов</summary><p>Сначала убедитесь, что слова относятся к этой записи. В карточке можно свести текст с аудио локальным ASR. Расчет требует времени и не гарантирует качество на музыке. Строки без надежной опоры останутся без времени; перед записью их нужно проверить.</p></details></>}
      {step === 'review' && <><p className="guide-lead">Проверьте найденное на нескольких треках, сохраните предложение и только затем запишите его в файл.</p><ol className="guide-checklist"><li>Откройте трек и сравните теги с исходными значениями.</li><li>Выберите подходящую обложку и текст. Прослушайте, проверьте версию записи и таймкоды.</li><li>Сохраните предложение. Это еще не меняет аудио.</li><li>Запишите проверенные изменения. Предыдущая версия сохраняется для отката.</li></ol><p className="muted">Актуальных сохраненных предложений: {number('current_proposals')}. Нет обязательного требования принять сомнительное совпадение: оставьте такой файл для ручной проверки.</p><div className="button-row"><button className="button primary" onClick={() => onNavigate('review')}>Открыть проверку треков<ArrowRight size={18}/></button><button className="button secondary" onClick={() => onNavigate('reports')}>Посмотреть, чего не хватает</button></div>{summary?.read_only && <div className="notice warning">Запись отключена текущим режимом хранилища.</div>}</>}
      {step === 'ready' && <><p className="guide-lead">Опубликуйте проверенные версии в готовую коллекцию, затем подготовьте экспорт для устройства.</p><div className="guide-facts"><div><strong>{number('published_current')}</strong><span>опубликованных текущих версий</span></div></div><p className="muted">Откройте план публикации в карточке трека или раздел готовой коллекции. Приложение проверит ревизию, метаданные и приемку текста. Публикация не означает, что файл уже доставлен на телефон.</p><div className="button-row"><button className="button primary" onClick={() => onNavigate('ready')}>Открыть готовую коллекцию<ArrowRight size={18}/></button><button className="button secondary" onClick={() => onNavigate('reports')}>Отчет по оставшимся файлам</button></div></>}
      {guideJobKind(step) && <div className="guide-operation">{active ? <><p role="status">Этот этап уже есть в обработке. Новую копию задачи создавать не нужно; пауза, ожидание источника и отложенный повтор тоже сохраняют задачу.</p><button className="button primary" onClick={() => onNavigate('jobs')}>Открыть ход обработки<ArrowRight size={18}/></button></> : <><button className="button primary" disabled={busy || submitting || !!disabledReason || loading} onClick={() => void start()}>{submitting ? 'Добавляем задачу...' : step === 'scan' ? 'Сканировать папку' : step === 'recognize' ? 'Распознать необработанные файлы' : 'Найти недостающие результаты'}<ArrowRight size={18}/></button>{disabledReason && <p className="muted small">{disabledReason}</p>}<button className="button subtle" onClick={() => onNavigate('jobs')}>Посмотреть задачи</button></>}</div>}
      <footer className="guide-footer"><button className="button secondary" disabled={!canBack} onClick={onBack}><ArrowLeft size={17}/>Назад</button>{index < guideSteps.length-1 && <button className="button secondary" onClick={() => change(guideSteps[index+1])}>Следующий шаг<ArrowRight size={17}/></button>}</footer>
    </section><p className="quiet-note">Шаг мастера показывает, где вы находитесь в инструкции. Он не является процентом готовности коллекции.</p>
  </div>;
}

const helpTopics = [
  { title: 'Самый короткий путь к результату', text: 'Папка -> сканирование -> распознавание -> поиск данных -> проверка и запись -> публикация. Начните с нескольких треков, чтобы проверить результат. Мастер показывает доступные действия и уже существующие задачи.' },
  { title: 'Предложение, файл и готовая коллекция - в чем разница?', text: 'Источник предлагает варианты. Сохраненное предложение - ваш черновик для конкретной версии файла. «Записать в файл» меняет музыкальные теги с проверками и резервной версией. Публикация создает отдельную готовую копию. Передача и воспроизведение на устройстве проверяются отдельно.' },
  { title: 'Что показывает состояние коллекции?', text: 'Теги, обложки и встроенный текст считаются отдельно от качества и проверки. Они могут пересекаться. Варианты поиска не равны сохраненным предложениям. Устаревший результат относится к прежней версии файла. Ошибка проверки аудио не всегда означает повреждение: файл может быть недоступен или иметь неподдерживаемый профиль.' },
  { title: 'Почему текст не получил таймкоды?', text: 'Сначала ASR независимо распознает слова, затем выравнивание ищет надежные совпадения с вашим текстом. Если слова не распознаны, текст относится к другой записи или совпадений недостаточно, строки остаются без времени. Проверьте запись, текст и язык. Техническое завершение задачи не означает, что караоке готово.' },
  { title: 'Почему поиск ничего не нашел или остановился?', text: 'Проверьте доступность источников в настройках и текущую задачу. Ключи, согласия, бюджет, ограничение частоты и временный отказ сервиса различаются. Продолжайте существующую задачу или повторяйте незавершенный этап. Неизвестную запись можно заполнить вручную; первый вариант поиска не обязательно верный.' },
  { title: 'Как работают отмена, процент и ETA?', text: 'Отмена сохраняет уже сделанное и безопасно завершает текущую атомарную операцию. Процент относится к обработанным попыткам, а не к организованности коллекции. ETA появляется, когда есть измерения; ожидание источника или холодный запуск могут изменить оценку. Текущие действия и события видны в обработке.' },
  { title: 'Как подключить папку или CUE?', text: 'Музыкальная папка подключается при установке приложения. В настройках показан путь, который видит приложение. После добавления файлов выполните сканирование. CUE хранит границы композиций; прослушивайте их из карточки альбома, а для отдельных файлов запустите извлечение. Облачные диски и загрузка папки с устройства пока не подключаются через этот интерфейс.' },
  { title: 'Что доступно без сети и после выхода?', text: 'PWA сохраняет интерфейс. Каталог, аудио и обработка требуют связи с сервером: вся музыка на устройство автоматически не скачивается. После выхода приватные экраны и плеер должны очиститься. «Назад» возвращает на одно действие внутри приложения; перезагрузка сохраняет текущий контекст, но не запускает звук сама.' },
];
export function HelpPage({ onNavigate }: { onNavigate: (page: Page) => void }) {
  return <div className="help-page"><section className="panel guide-intro"><div className="guide-intro-icon"><BookOpen size={26}/></div><div><h2>От первого сканирования до прослушивания</h2><p>Необязательно знать все разделы. Мастер проведет по основному пути и предложит следующий шаг.</p></div><button className="button primary" onClick={() => onNavigate('guide')}>Открыть мастер<ArrowRight size={18}/></button></section><section className="panel help-topics"><h2>Как пользоваться Фонотекой</h2>{helpTopics.map(topic => <details key={topic.title}><summary>{topic.title}</summary><p>{topic.text}</p></details>)}</section><div className="button-row"><button className="button secondary" onClick={() => onNavigate('reports')}>Состояние коллекции</button><button className="button secondary" onClick={() => onNavigate('jobs')}>Ход обработки</button><button className="button secondary" onClick={() => onNavigate('settings')}>Настройки источников</button></div></div>;
}
