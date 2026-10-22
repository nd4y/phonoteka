import { ArrowRight, CheckCheck, FileAudio, FolderCheck, Image, ListMusic, Tags, TriangleAlert } from 'lucide-react';
import './CollectionOverview.css';

export type CollectionFilter = 'all' | 'missing_art' | 'missing_lyrics' | 'audit_errors';
type Props = { counts: Readonly<Record<string, number>>; onBrowse?: (filter: CollectionFilter) => void; onGuide?: () => void };
export const countValue = (value: unknown): number | null => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
const formatted = (value: unknown) => { const number = countValue(value); return number === null ? '-' : number.toLocaleString('ru-RU'); };
export function coverage(value: unknown, total: unknown): number | null {
  const count = countValue(value), denominator = countValue(total);
  return count !== null && denominator !== null && denominator > 0 && count <= denominator ? Math.round(count * 100 / denominator) : null;
}
const groups = [
  { title: 'Что означают результаты поиска', note: 'Поиск относится к конкретной версии файла. После записи тегов прежний результат может стать устаревшим. Вариант источника еще не является сохраненным предложением.', fields: { analyzed: 'Поиск для текущей версии', stale_analysis: 'Поиск для прежней версии', with_candidates: 'Файлы с вариантами от источников', no_match: 'Поиск без совпадений', asr_drafts: 'Файлы с черновиком ASR', writable_profiles: 'Формат поддерживает запись' } },
  { title: 'Аудиоотпечатки и внешние источники', note: 'Совпадение отпечатка помогает определить запись. Версию, релиз и предлагаемые поля нужно проверить. Временный отказ можно повторить позже.', fields: { fingerprint_matches: 'Отпечаток совпал', fingerprint_no_match: 'Отпечаток не найден', fingerprint_unavailable: 'Распознавание недоступно', metadata_pending_retry: 'Есть незавершенные запросы метаданных' } },
  { title: 'Происхождение из YouTube', note: 'Ссылка из исходного файла и подтвержденная владельцем загрузка отличаются от найденного похожего видео. Кандидат не доказывает, откуда скачан файл.', fields: { youtube_recorded: 'Ссылка в исходных данных', youtube_confirmed: 'Загрузка подтверждена владельцем', youtube_candidates: 'Есть кандидаты видео', youtube_conflicts: 'Противоречащие ссылки' } },
];

export function CollectionOverview({ counts, onBrowse, onGuide }: Props) {
  const total = countValue(counts.audio_files);
  const metrics = [
    { key: 'with_core_tags', title: 'Основные теги', detail: 'Название, исполнитель и альбом', icon: Tags },
    { key: 'with_cover', title: 'Обложки', detail: 'Изображение внутри аудиофайла', icon: Image, filter: 'missing_art' as const },
    { key: 'with_lyrics', title: 'Текст с таймкодами', detail: 'Наличие текста, без оценки точности', icon: ListMusic, filter: 'missing_lyrics' as const },
  ];
  return <div className="collection-overview">
    <div className="overview-intro"><div><div className="eyebrow">ОТ ПАПКИ К ГОТОВОЙ МУЗЫКЕ</div><h2>Что уже есть и что дальше</h2><p className="muted">Заполненность файлов, результаты проверки и следующий шаг - по отдельности.</p></div>{onGuide && <button className="button primary" onClick={onGuide}>Продолжить с мастером<ArrowRight size={18}/></button>}</div>
    <div className="overview-inventory"><FileAudio size={28}/><div><strong>{formatted(total)}</strong><span>аудиофайлов в каталоге</span></div><p>Всего {formatted(counts.files)} записей, включая {formatted(counts.cue_sheets)} документов CUE. Документы с границами композиций не входят в заполненность аудио.</p>{onBrowse && <button className="button subtle" onClick={() => onBrowse('all')}>Открыть коллекцию<ArrowRight size={17}/></button>}</div>
    <div className="overview-metrics">{metrics.map(metric => {
      const percent = coverage(counts[metric.key], total), value = countValue(counts[metric.key]);
      return <section className="overview-metric" key={metric.key} aria-label={metric.title}><metric.icon size={22}/><h3>{metric.title}</h3><div className="overview-quantity"><strong>{formatted(value)}</strong><span>из {formatted(total)}</span>{percent !== null && <b>{percent}%</b>}</div>{percent !== null ? <progress aria-label={metric.title} value={value!} max={total!}/> : <div className="overview-empty-meter" aria-hidden="true"/>}<p className="muted small">{metric.detail}</p>{metric.filter && onBrowse && value !== null && total !== null && value < total && <button className="text-button" onClick={() => onBrowse(metric.filter!)}>Показать, где не хватает</button>}</section>;
    })}</div>
    <p className="overview-explanation muted small">Наличие данных не подтверждает правильность тегов или таймкодов. Показатели могут пересекаться - их не нужно складывать в общий процент готовности.</p>
    <div className="overview-actions"><section className={`overview-action ${counts.audit_errors > 0 ? 'attention' : ''}`}><TriangleAlert size={23}/><div><strong>{formatted(counts.audit_errors)}</strong><h3>Ошибки проверки аудио</h3><p>Файл не прочитан или не прошел проверку. Это еще не доказательство повреждения.</p><span className="muted small">Из них ошибки первичного чтения: {formatted(counts.input_errors)}. Не проверено: {formatted(counts.audit_unchecked)} · С предупреждениями: {formatted(counts.audit_warnings)}</span></div>{onBrowse && counts.audit_errors > 0 && <button className="button secondary" onClick={() => onBrowse('audit_errors')}>Разобрать ошибки</button>}</section>
    <section className="overview-action"><CheckCheck size={23}/><div><strong>{formatted(counts.current_proposals)}</strong><h3>Сохраненные предложения</h3><p>Подготовлены для текущих версий файлов. Их еще нужно проверить перед записью.</p></div>{onGuide && <button className="button secondary" onClick={onGuide}>Что делать дальше</button>}</section>
    <section className="overview-action"><FolderCheck size={23}/><div><strong>{formatted(counts.published_current)}</strong><h3>Опубликованные версии</h3><p>Текущие версии скопированы в готовую коллекцию. Доставка на устройство проверяется отдельно.</p></div></section></div>
    <details className="overview-details"><summary>Подробности поиска и источников</summary><div className="overview-detail-grid">{groups.map(group => <section key={group.title}><h3>{group.title}</h3><p className="muted small">{group.note}</p><dl>{Object.entries(group.fields).map(([key,label]) => <div key={key}><dt>{label}</dt><dd>{formatted(counts[key])}</dd></div>)}</dl></section>)}</div></details>
  </div>;
}
