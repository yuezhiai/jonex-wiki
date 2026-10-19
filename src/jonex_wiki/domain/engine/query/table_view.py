"""Deterministic display of Markdown cells; never an answer or new citation.

Column indexes and explicit empty cells are retained. Date serials are decoded
only in a column-header row paired with weekday labels, not in arbitrary data.
"""
from __future__ import annotations

import re
import hashlib
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from typing import Any, Iterable


def _cells(line: str) -> list[str]:
    return [cell.strip().replace(r'\|', '|') for cell in
            re.split(r'(?<!\\)\|', line.strip()[1:-1])]


def table_view(text: str) -> list[dict]:
    sections = []
    heading = ''
    rows = []

    def flush():
        if len(rows) < 2:
            rows.clear()
            return
        separator = next((i for i, row in enumerate(rows)
                          if all(re.fullmatch(r':?-+:?', cell.replace(' ', '')) for cell in row)), None)
        if separator is None or separator == 0:
            rows.clear()
            return
        header = rows[:separator]
        data = rows[separator + 1:]
        # Explicit repeated leading labels distinguish an Office header tier
        # from ordinary values. Preserve both parent and leaf labels.
        while data and len(header[0]) >= 3 and all(header[0][:2]) and data[0][:2] == header[0][:2] and data[0] != header[0]:
            header.append(data.pop(0))
        # Office exports often put an instructional tier above the real field
        # names.  An explicit sequence field is structural evidence; it must
        # not become a counted business row merely because its first labels do
        # not repeat the instructional text.
        while data and any(cell.strip() in {'序号', '编号'} for cell in data[0]):
            header.append(data.pop(0))
        # A secondary calendar header is data-shaped but explicitly contains
        # weekdays or Excel date serials and blank leading row-label columns.
        while data and not any(data[0][:2]) and (
            any(re.fullmatch(r'星期[一二三四五六日天]', c) for c in data[0]) or
            any(re.fullmatch(r'[345]\d{4}(?:\.0)?', c) for c in data[0])
        ):
            header.append(data.pop(0))
        width = max(map(len, rows))
        labels = []
        calendar = any(re.fullmatch(r'星期[一二三四五六日天]', c) for row in header for c in row)
        for column in range(width):
            parts = list(dict.fromkeys(row[column] for row in header if column < len(row) and row[column]))
            if calendar:
                for part in list(parts):
                    if re.fullmatch(r'[345]\d{4}(?:\.0)?', part):
                        parts.append((datetime(1899, 12, 30) + timedelta(days=int(float(part)))).date().isoformat())
            labels.append({'index': column, 'labels': parts})
        raw_header = [list(row) for row in header]
        header_key = hashlib.sha256(repr(raw_header).encode('utf-8')).hexdigest()[:16]
        base_header_key = hashlib.sha256(repr(raw_header[:1]).encode('utf-8')).hexdigest()[:16]
        sections.append({'section': heading, 'columns': labels,
                         'header_rows': raw_header, 'header_key': header_key,
                         'base_header_key': base_header_key,
                         'rows': [{'row': i+1, 'cells': row} for i, row in enumerate(data)],
                         'row_count': len(data),
                         'width_mismatch': any(len(row) != width for row in data)})
        rows.clear()

    for line in text.splitlines():
        if line.strip().startswith('|') and line.strip().endswith('|'):
            rows.append(_cells(line))
        else:
            flush()
            if line.startswith('#'):
                heading = line.lstrip('# ').strip()
    flush()
    return sections


_QUESTION_NOISE = {
    '哪些', '哪个', '多少', '几个', '是否', '怎么', '什么', '分别', '一共',
    '事项', '权限', '要求', '情况', '处理', '里面', '其中',
}


def _question_terms(question: str) -> set[str]:
    compact = re.sub(r'\s+', '', question).casefold()
    chunks = re.findall(r'[A-Za-z]+\d*|\d+(?:\.\d+)?%?|[\u4e00-\u9fff]+', compact)
    terms = set()
    for chunk in chunks:
        if re.fullmatch(r'[\u4e00-\u9fff]+', chunk):
            for size in range(2, min(6, len(chunk)) + 1):
                terms.update(chunk[i:i + size] for i in range(len(chunk) - size + 1))
        elif len(chunk) >= 2:
            terms.add(chunk)
    return terms - _QUESTION_NOISE


def _maximal_hits(terms: set[str], text: str) -> list[str]:
    matches = {term for term in terms if term in text}
    return sorted(
        (term for term in matches if not any(term != other and term in other for other in matches)),
        key=lambda value: (-len(value), value),
    )


def table_analysis_records(
    records: Iterable[Any], question: str, *, max_documents: int = 2,
    max_columns: int = 4, max_values: int = 32,
) -> list[dict]:
    """Aggregate question-relevant table statistics across passage fragments.

    Rows are deduplicated by their source passage and exact cells. Statistics
    stay within one document and column index, preventing unrelated tables from
    being mixed while allowing a large table split across passages to be counted.
    """
    terms = _question_terms(question)
    grouped: dict[tuple[str, int], dict[str, Any]] = {}
    document_scores: Counter[str] = Counter()
    document_rows: dict[str, set[tuple[str, tuple[str, ...]]]] = defaultdict(set)
    for record in records:
        document_id = str(getattr(record, 'document_id', ''))
        passage_id = str(getattr(record, 'passage_id', ''))
        excerpt = str(getattr(record, 'excerpt', ''))
        for view in table_view(excerpt):
            rows = view['rows']
            for row in rows:
                document_rows[document_id].add((passage_id, tuple(row['cells'])))
            for column in view['columns']:
                index = column['index']
                labels = [str(value) for value in column['labels']]
                values = [row['cells'][index] if index < len(row['cells']) else '' for row in rows]
                label_hits = _maximal_hits(terms, '/'.join(labels).casefold())
                value_hits = _maximal_hits(terms, '\n'.join(values).casefold())
                score = sum(len(term) ** 2 for term in label_hits) * 4 + sum(len(term) ** 2 for term in value_hits)
                if not score:
                    continue
                key = (document_id, index)
                entry = grouped.setdefault(key, {'labels': [], 'hits': set(), 'score': 0})
                entry['labels'] = list(dict.fromkeys(entry['labels'] + labels))
                entry['hits'].update(label_hits + value_hits)
                entry['score'] += score
                document_scores[document_id] += score

    selected_documents = {doc for doc, _ in document_scores.most_common(max_documents)}
    output = []
    for document_id in selected_documents:
        columns = sorted(
            ((data['score'], index, data) for (doc, index), data in grouped.items() if doc == document_id),
            key=lambda item: (-item[0], item[1]),
        )[:max_columns]
        if not columns:
            continue
        indexes = [index for _, index, _ in columns]
        rows = sorted(document_rows[document_id])
        profiles = []
        for _score, index, data in columns:
            counts = Counter(cells[index] for _, cells in rows if index < len(cells) and cells[index])
            profiles.append({
                'index': index, 'labels': data['labels'],
                'matched_question_terms': sorted(data['hits'], key=lambda value: (-len(value), value))[:12],
                'non_empty_count': sum(counts.values()), 'distinct_count': len(counts),
                'value_counts': [
                    {'value': value[:160], 'count': count}
                    for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:max_values]
                ],
            })
        joint = Counter(
            tuple(cells[index] if index < len(cells) else '' for index in indexes)
            for _, cells in rows
        )
        output.append({
            'document_id': document_id, 'row_count': len(rows),
            'column_profiles': profiles,
            'joint_counts': [
                {'values': [{'index': index, 'value': value[:160]} for index, value in zip(indexes, key)], 'count': count}
                for key, count in sorted(joint.items(), key=lambda item: (-item[1], item[0]))[:max_values]
            ],
        })
    return sorted(output, key=lambda item: (-document_scores[item['document_id']], item['document_id']))


def table_projection(text: str, question: str, *, max_rows: int = 64) -> list[dict]:
    """Return one compact table representation without duplicating Markdown."""
    terms = _question_terms(question)
    dates = {
        '-{:02d}-{:02d}'.format(int(month), int(day))
        for month, day in re.findall(r'(\d{1,2})月(\d{1,2})日', question)
    }
    output = []
    meal_alias = {'早': '早餐', '中': '午餐', '晚': '晚餐'}
    for view in table_view(text):
        columns = view['columns']
        relevant = []
        for column in columns:
            label = '/'.join(column['labels']).casefold()
            if _maximal_hits(terms, label) or any(date in label for date in dates):
                relevant.append(column['index'])
        row_matches = []
        for row in view['rows']:
            cells = row['cells']
            row_key = '/'.join(meal_alias.get(value, value) for value in cells[:2]).casefold()
            if _maximal_hits(terms, row_key):
                row_matches.append(row)
        indexes = sorted(set(range(min(5, len(columns)))) | set(relevant))
        rows = row_matches or view['rows']
        if not indexes or not rows:
            continue
        output.append({
            'section': view['section'],
            'columns': [
                {'index': index, 'header_sequence': columns[index]['labels']}
                for index in indexes
            ],
            'rows': [
                {'row': row['row'], 'cells': [
                    {'index': index, 'value': (row['cells'][index] if index < len(row['cells']) else '')[:240]}
                    for index in indexes
                ]}
                for row in rows[:max_rows]
            ],
            'rows_omitted': max(0, len(rows) - max_rows),
        })
    return output


def table_analysis(text: str, question: str, *, max_columns: int = 4) -> list[dict]:
    """Return bounded, deterministic statistics for question-relevant columns.

    This projection never decides the answer. It exposes exact per-column and
    joint counts, plus matching row keys, so synthesis does not have to count
    dozens of Markdown rows by eye.
    """
    views = table_view(text)
    terms = _question_terms(question)
    output = []
    for view in views:
        rows = view['rows']
        if not rows:
            continue
        scored = []
        for column in view['columns']:
            index = column['index']
            label = '/'.join(column['labels']).casefold()
            values = [row['cells'][index] if index < len(row['cells']) else '' for row in rows]
            value_text = '\n'.join(values).casefold()
            label_hits = _maximal_hits(terms, label)
            value_hits = _maximal_hits(terms, value_text)
            score = sum(len(term) ** 2 for term in label_hits) * 4 + sum(
                len(term) ** 2 for term in value_hits
            )
            if score:
                scored.append((score, index, column, values, label_hits, value_hits))
        chosen = sorted(scored, key=lambda item: (-item[0], item[1]))[:max_columns]
        if not chosen:
            continue
        profiles = []
        for _score, index, column, values, label_hits, value_hits in chosen:
            counts = Counter(value for value in values if value)
            profiles.append({
                'index': index,
                'labels': column['labels'],
                'matched_question_terms': sorted(set(label_hits + value_hits), key=lambda x: (-len(x), x))[:12],
                'non_empty_count': sum(bool(value) for value in values),
                'distinct_count': len(counts),
                'value_counts': [
                    {'value': value, 'count': count}
                    for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:64]
                ],
            })
        joint = []
        indexes = [item[1] for item in chosen]
        if len(indexes) >= 2:
            groups = {}
            for ordinal, row in enumerate(rows, start=1):
                cells = row['cells']
                key = tuple(cells[index] if index < len(cells) else '' for index in indexes)
                row_key = cells[0] if cells else str(ordinal)
                group = groups.setdefault(key, {'count': 0, 'row_keys': []})
                group['count'] += 1
                if len(group['row_keys']) < 64:
                    group['row_keys'].append(row_key or str(ordinal))
            joint = [
                {'values': [{'index': index, 'value': value} for index, value in zip(indexes, key)], **group}
                for key, group in sorted(groups.items(), key=lambda item: (-item[1]['count'], item[0]))[:64]
            ]
        output.append({
            'section': view['section'],
            'row_count': view['row_count'],
            'column_profiles': profiles,
            'joint_counts': joint,
        })
    return output
