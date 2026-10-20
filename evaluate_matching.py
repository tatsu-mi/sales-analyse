"""Compare saved assessments with human-reviewed labels; no API calls."""
import argparse
import json
from pathlib import Path


STATES = {'match', 'mismatch', 'unknown'}


def indexed(records, state_key):
    result = {}
    if not isinstance(records, list) or not records:
        raise ValueError('評価データは空でないJSON配列にしてください。')
    for record in records:
        key = tuple(record[field] for field in ('engineer', 'uid', 'project_key'))
        if (not all(isinstance(value, str) for value in key) or not key[0] or not key[1]
                or record[state_key] not in STATES or key in result):
            raise ValueError('評価データのキー・状態・重複を確認してください。')
        result[key] = record
    return result


def evaluate(predictions, labels):
    predictions, labels = indexed(predictions, 'state'), indexed(labels, 'expected')
    if labels.keys() - predictions.keys():
        raise ValueError('正解ラベルに対応する判定記録がありません。対象実行のファイルを確認してください。')
    pairs = [(predictions[key]['state'], label['expected']) for key, label in labels.items()]
    true_matches = sum(pred == gold == 'match' for pred, gold in pairs)
    recommended = sum(pred == 'match' for pred, _ in pairs)
    actual_matches = sum(gold == 'match' for _, gold in pairs)
    return {
        'labeled_count': len(pairs),
        'unlabeled_count': len(predictions) - len(labels),
        'accuracy': sum(pred == gold for pred, gold in pairs) / len(pairs),
        'match_precision': true_matches / recommended if recommended else None,
        'match_recall': true_matches / actual_matches if actual_matches else None,
        'false_recommendations': sum(pred == 'match' and gold != 'match' for pred, gold in pairs),
        'unknown_rate': sum(pred == 'unknown' for pred, _ in pairs) / len(pairs),
        'disagreements': [dict(engineer=key[0], uid=key[1], project_key=key[2],
                               predicted=predictions[key]['state'], expected=label['expected'])
                          for key, label in labels.items() if predictions[key]['state'] != label['expected']],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('assessments', type=Path)
    parser.add_argument('labels', type=Path)
    args = parser.parse_args()
    try:
        result = evaluate(json.loads(args.assessments.read_text(encoding='utf-8-sig')),
                          json.loads(args.labels.read_text(encoding='utf-8-sig')))
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(1, '評価に失敗しました。JSON配列・キー・状態・重複・対応する実行を確認してください。\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
