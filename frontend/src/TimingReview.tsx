import { useMemo, useState } from 'react';
import { CircleAlert, Headphones, MoveRight, Play, Rows3 } from 'lucide-react';
import { formatTime } from './lyrics';
import { compareTiming, timingDelta, TIMING_REVIEW_RULES } from './timing';
import type { AlignmentTimingLine, TimingComparison, TimingReport } from './timing';
import './TimingReview.css';

interface TimingReviewProps {
  alignment?: { lines: readonly AlignmentTimingLine[] } | null;
  duration?: number;
  stale?: boolean;
  disabled?: boolean;
  onPreview?: (seconds: number) => void;
  onAcceptLine?: (alignmentLineIndex: number, proposedMs: number) => void;
  onSuggestShift?: (deltaMs: number) => void;
}
const patterns = {
  unknown: 'Тип расхождения неизвестен',
  stable: 'Заметного расхождения в сопоставленных строках нет',
  global_shift: 'Похоже на общий сдвиг сопоставленных строк',
  local_changes: 'Есть локальные отклонения таймкодов',
};
const regionNames = { start: 'Начало участка', middle: 'Середина участка', end: 'Конец участка' };
const issues = {
  original_missing: 'Нет исходного времени для сравнения',
  anchor_missing: 'Нет надёжного якоря в аудио',
  invalid_time: 'Некорректное время',
  outside_audio: 'Время выходит за длительность аудио',
};

function TimingPlot({ report }: { report: TimingReport }) {
  const points = report.lines.filter(line => line.residualMs !== null);
  if (points.length < TIMING_REVIEW_RULES.minimumComparisons) return null;
  const first = Math.min(...points.map(line => line.originalMs!));
  const span = Math.max(...points.map(line => line.originalMs!)) - first || 1;
  const limit = Math.max(1000, ...points.map(line => Math.abs(line.residualMs!)));
  return <div className="timing-plot"><svg viewBox="0 0 600 112" role="img" aria-label="Отклонения от медианного сдвига по сопоставленному участку. Точки не измеряют точность распознавания пения.">
    <line x1="12" y1="56" x2="588" y2="56" className="timing-zero" />
    <line x1="204" y1="10" x2="204" y2="102" className="timing-region-line" /><line x1="396" y1="10" x2="396" y2="102" className="timing-region-line" />
    {points.map(line => <circle key={line.index} cx={12 + (line.originalMs! - first) / span * 576} cy={56 - line.residualMs! / limit * 42} r={line.outlier ? 4 : 3} className={line.outlier ? 'outlier' : ''}><title>Строка {line.index + 1}: отклонение от медианы {timingDelta(line.residualMs)}; старое время {formatTime(line.originalMs)}</title></circle>)}
  </svg><p>Отклонения от медианного сдвига: начало, середина и конец сопоставленного участка.</p></div>;
}

function LineReview({ line, disabled, onPreview, onAcceptLine }: { line: TimingComparison; disabled: boolean; onPreview?: TimingReviewProps['onPreview']; onAcceptLine?: TimingReviewProps['onAcceptLine'] }) {
  const proposedValid = line.proposedMs !== null && line.issue !== 'outside_audio' && line.issue !== 'invalid_time';
  return <tr className={line.outlier ? 'timing-outlier' : line.issue ? 'timing-unpaired' : ''}>
    <td className="timing-line-number">{line.index + 1}</td><td className="timing-line-text">{line.text || <span className="muted">Пустая строка</span>}{line.issue && <small>{issues[line.issue]}</small>}{line.outlier && <small><CircleAlert size={12} /> Большое локальное отклонение, проверьте по аудио</small>}</td>
    <td><button className="timing-time" disabled={disabled || !onPreview || line.originalMs === null || line.issue === 'outside_audio' || line.issue === 'invalid_time'} onClick={() => onPreview?.(line.originalMs! / 1000)} aria-label={`Перейти к исходному времени строки ${line.index + 1}`}><Play size={11} />{line.originalMs === null ? 'Нет времени' : formatTime(line.originalMs)}</button></td>
    <td><button className="timing-time proposed" disabled={disabled || !onPreview || !proposedValid} onClick={() => onPreview?.(line.proposedMs! / 1000)} aria-label={`Перейти к предложенному времени строки ${line.index + 1}`}><Play size={11} />{line.proposedMs === null ? 'Нет якоря' : formatTime(line.proposedMs)}</button></td>
    <td className="timing-delta">{line.deltaMs === null ? 'Неизвестно' : timingDelta(line.deltaMs)}</td>
    {onAcceptLine && <td><button className="text-button" disabled={disabled || !proposedValid || line.issue === 'anchor_missing'} onClick={() => onAcceptLine(line.index, line.proposedMs!)}>В черновик</button></td>}
  </tr>;
}

export default function TimingReview({ alignment, duration, stale = false, disabled = false, onPreview, onAcceptLine, onSuggestShift }: TimingReviewProps) {
  const [onlyIssues, setOnlyIssues] = useState(false);
  const report = useMemo(() => compareTiming(alignment?.lines || [], duration, stale), [alignment, duration, stale]);
  if (!alignment) return null;
  const blocked = disabled || stale;
  const issueCount = report.lines.filter(line => line.issue || line.outlier).length;
  const visible = onlyIssues ? report.lines.filter(line => line.issue || line.outlier) : report.lines;
  return <section className="timing-review" aria-label="Сравнение исходных и предложенных таймкодов">
    <div className="timing-heading"><Headphones size={19} /><div><h3>Проверка таймкодов</h3><p>Исходная версия и слова сохранены. Новые времена остаются предложением.</p></div><span className={`badge ${report.pattern === 'local_changes' ? 'warm' : 'purple'}`}>{report.compared} из {report.lines.length} строк для сравнения</span></div>
    <p className="timing-pattern"><strong>{patterns[report.pattern]}</strong></p>
    {stale && <p className="timing-warning"><CircleAlert size={14} />Аудио или текст изменились. Повторите выравнивание перед сравнением и принятием времён.</p>}
    {!report.monotonic && <p className="timing-warning"><CircleAlert size={14} />Нарушен порядок таймкодов. Общий сдвиг не предлагается, проверьте строки вручную.</p>}
    {report.compared < TIMING_REVIEW_RULES.minimumComparisons && <p className="muted small">Для различения общего сдвига и локальных изменений нужно не менее трёх строк с исходным временем и якорем в аудио. Пропуски не интерполируются.</p>}
    <dl className="timing-metrics"><div><dt>Медианный сдвиг</dt><dd>{timingDelta(report.medianOffsetMs)}</dd></div><div><dt>Отклонение от медианы, p95</dt><dd>{report.residualP95Ms === null ? 'Неизвестно' : `${(report.residualP95Ms / 1000).toFixed(3)} с`}</dd></div><div><dt>Изменение от начала к концу</dt><dd>{timingDelta(report.regionalDriftMs)}</dd></div><div><dt>Локальные выбросы</dt><dd>{report.compared >= TIMING_REVIEW_RULES.minimumComparisons ? report.outlierCount : 'Неизвестно'}</dd></div></dl>
    <TimingPlot report={report} />
    <div className="timing-regions">{report.regions.map(region => <div key={region.name}><span>{regionNames[region.name]}</span><strong>{timingDelta(region.medianDeltaMs)}</strong><small>{region.count} строк для сравнения</small></div>)}</div>
    {report.regionalDriftMs === null && <p className="muted small">Региональный дрейф неизвестен: нужны минимум две сопоставленные строки в каждой трети участка протяжённостью от 10 секунд.</p>}
    {report.pattern === 'local_changes' && <p className="timing-warning">Одного общего сдвига недостаточно. Прослушайте участки и проверьте предложенные построчные времена.</p>}
    {onSuggestShift && report.globalShiftSuggestionMs !== null && <button className="button secondary timing-suggest" disabled={blocked} onClick={() => onSuggestShift(report.globalShiftSuggestionMs!)}><MoveRight size={16} />Сдвиг {timingDelta(report.globalShiftSuggestionMs)} в черновик</button>}
    {!!report.unpaired && <p className="muted small">Для {report.unpaired} строк сравнение неизвестно. Предложение общего сдвига для всего текста недоступно.</p>}
    <div className="timing-table-toolbar"><Rows3 size={15} /><strong>Старое и новое время</strong><label><input type="checkbox" checked={onlyIssues} onChange={event => setOnlyIssues(event.target.checked)} />Только пропуски и выбросы ({issueCount})</label></div>
    <div className="timing-table-scroll" tabIndex={0} aria-label="Построчное сравнение таймкодов"><table><thead><tr><th scope="col">№</th><th scope="col">Строка</th><th scope="col">Исходное</th><th scope="col">Предложено</th><th scope="col">Разница</th>{onAcceptLine && <th scope="col">Принятие</th>}</tr></thead><tbody>{visible.map(line => <LineReview key={line.index} line={line} disabled={blocked} onPreview={onPreview} onAcceptLine={onAcceptLine} />)}</tbody></table>{!visible.length && <p className="timing-no-lines">{report.lines.length ? 'Пропусков и больших выбросов не обнаружено. Проверьте вокал по аудио.' : 'Выравнивание пока не содержит строк.'}</p>}</div>
    <details className="timing-rules"><summary>Как читать сравнение</summary><p>Разница = предложенное время минус исходное. Плюс означает более позднюю подсветку. Это различия двух наборов времён, а не измеренная ошибка по эталону и не оценка правильности пения. Совпадение не присваивает статус проверки человеком или в Poweramp.</p><p>Общий сдвиг предполагается, когда минимум три сравнения отличаются от медианы не более чем на 300 мс. Выброс: отклонение более max(800 мс, 3 × 1,4826 × MAD). Региональный дрейф сравнивает медианы первой и последней трети сопоставленного участка; изменение от 600 мс требует построчной проверки. Это правила отбора для прослушивания, музыкальная точность модели ими не подтверждается.</p><p>Переход по времени не меняет файл. «В черновик» переносит только выбранные времена, окончательная запись требует обычного сохранения и применения предложения.</p></details>
  </section>;
}
