"""Describe independent audio anchors without exporting lyrics or inventing timestamps."""
from statistics import median


def summarize(lines):
    details = []
    for number, line in enumerate(lines, 1):
        if not line['text'].strip():
            continue
        reference = line.get('original_ms')
        matched = line.get('start_ms') if line['state'] == 'anchored' else None
        delta = matched - reference if matched is not None and reference is not None else None
        details.append({'line': number, 'reference_ms': reference, 'matched_ms': matched, 'delta_ms': delta,
                        'matched_words': line.get('matched_words', 0), 'words': line.get('words', 0)})
    deltas = [line['delta_ms'] for line in details if line['delta_ms'] is not None]
    offset = median(deltas) if deltas else None
    maximum = max(map(abs, deltas), default=None)
    residual = max((abs(d - offset) for d in deltas), default=None)
    classification = 'no_anchors'
    if deltas:
        if len(deltas) != len(details):
            classification = 'partial'
        elif maximum <= 500:
            classification = 'consistent'
        elif len(deltas) >= 3 and abs(offset) > 500 and residual <= 250:
            classification = 'constant_offset'
        else:
            classification = 'timing_disagreement'
    return {'classification': classification, 'line_details': details, 'lines': len(details),
            'anchored': len(deltas), 'coverage': len(deltas) / max(len(details), 1),
            'median_ms': median(map(abs, deltas)) if deltas else None, 'offset_ms': offset,
            'max_ms': maximum, 'max_residual_ms': residual}

def recover_unique_lines(lines, words, duration, unscored):
    """Retry a missed occurrence between independent neighboring anchors, without consulting the LRC time."""
    from bisect import bisect_left, bisect_right
    from collections import Counter, defaultdict
    from .providers import normalized
    tokens, starts, ends, occurrences = [], [], [], defaultdict(list)
    for word in words:
        if (not unscored or 'probability' in word) and word.get('probability', 0) < .5:
            continue
        start, end = word.get('start', -1), word.get('end', -1)
        if not 0 <= start <= end <= duration + .1:
            continue
        for token in normalized(word.get('word', '')).split():
            if starts and start < starts[-1]:
                return lines
            occurrences[token].append(len(tokens))
            tokens.append(token)
            starts.append(start)
            ends.append(end)
    upper = duration + .1
    bounds = []
    for line in reversed(lines):
        bounds.append(upper)
        if line['state'] == 'anchored':
            upper = line['start_ms'] / 1000
    lower = 0
    result = []
    unresolved = Counter(normalized(line['text']) for line in lines if line['state'] != 'anchored')
    for line, upper in zip(lines, reversed(bounds)):
        if line['state'] != 'anchored':
            phrase = normalized(line['text']).split()
            if 3 <= len(phrase) <= 100 and unresolved[' '.join(phrase)] == 1:
                positions = occurrences[phrase[0]]
                begin = bisect_left(positions, bisect_left(starts, lower))
                stop = bisect_left(positions, bisect_right(starts, upper))
                hits = []
                for index in positions[begin:stop]:
                    last = index + len(phrase) - 1
                    if last >= len(ends) or ends[last] > upper or tokens[index:last+1] != phrase:
                        continue
                    if ends[last] - starts[index] <= max(15, len(phrase)*2):
                        hits.append((starts[index], ends[last]))
                        if len(hits)>1:
                            break
                if len(hits)==1:
                    start,end=hits[0]
                    line={**line,'state':'anchored','matched_words':len(phrase),'start_ms':round(start*1000),
                          'end_ms':round(end*1000),'recovered':True}
        if line['state']=='anchored':
            lower=max(lower,line['end_ms']/1000)
        result.append(line)
    return result
