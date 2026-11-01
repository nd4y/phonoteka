import type { Analysis, Track } from './types';

export function instrumentalTextConflict(track: Track): boolean {
  const title = track.tags.title;
  const value = Array.isArray(title) ? title.join(' ') : String(title || '');
  return /\binstrumental\b|(?:^|[^\p{L}])инструментал(?:ьная|ьный|ьное)?(?=$|[^\p{L}])/iu.test(value)
    && !!(track.lrc || track.lyrics || track.proposal?.lrc || track.analysis?.candidates.some(candidate => candidate.kind === 'lyrics' && (candidate.lrc || candidate.plain_lyrics)));
}

export function alignmentStatus(analysis?: Analysis | null) {
  const alignment = analysis?.alignment;
  if (!alignment) return null;
  const words = analysis?.asr?.words || [], reliable = words.filter(word => word.probability >= .5);
  const aligned = alignment.lines.filter(line => typeof line.start_ms === 'number' && Number.isFinite(line.start_ms) && line.start_ms >= 0).length;
  const total = alignment.lines.length;
  if (!total) return { code: 'empty_reference', title: 'В тексте нет строк для выравнивания', detail: 'Добавьте содержательный текст этой записи и сохраните предложение перед новым расчетом.' };
  if (!words.length) return { code: 'no_recognized_words', title: 'Распознанных слов нет',
    detail: `Выравнивание не получило независимых опор для ${total} строк. Это не доказывает отсутствие вокала: проверьте версию записи, соответствие текста и язык.` };
  if (!aligned && alignment.lines.every(line => line.reason === 'independent_anchor_missing')) return {
    code: 'no_independent_anchors', title: 'Слова аудио не дали надежных совпадений с текстом',
    detail: `ASR вернул ${words.length} слов, из них ${reliable.length} с достаточной уверенностью. Ни одна из ${total} строк не получила полной независимой опоры. Проверьте слова, язык и конкретную версию записи.` };
  return { code: aligned === total && total > 0 ? 'requires_listening' : 'partial_alignment',
    title: aligned === total && total > 0 ? 'Времена рассчитаны, нужна проверка по аудио' : 'Таймкоды получены только для части строк',
    detail: `${aligned} из ${total} строк имеют время. Остальные не заполняются интерполяцией. Даже полное покрытие не подтверждает правильность слов и точность начала строки.` };
}
