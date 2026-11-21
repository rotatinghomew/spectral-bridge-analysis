from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

CONFIG = Path(__file__).with_name('sources.json')


def load_sources(directory):
    """读入交换资料，检查表头、行宽、身份键及显式引用，不作业务判断。"""
    config = json.loads(CONFIG.read_text(encoding='utf-8'))
    tables = {}
    for name, fields in config['tables'].items():
        with (directory / (name + '.csv')).open(encoding='utf-8', newline='') as file:
            reader = csv.DictReader(file)
            if reader.fieldnames != fields['columns']:
                raise ValueError(f'{name} 的表头与交换约定不同')
            rows = list(reader)
        keys = set()
        for index, row in enumerate(rows, 2):
            if None in row or None in row.values():
                raise ValueError(f'{name} 第 {index} 行的字段数不匹配')
            key = tuple(row[column] for column in fields['key'])
            if key in keys or not all(key):
                raise ValueError(f'{name} 第 {index} 行的标识为空或重复')
            keys.add(key)
        tables[name] = rows
    for source, column, target, target_column in config['links']:
        known = {row[target_column] for row in tables[target]}
        for index, row in enumerate(tables[source], 2):
            if row[column] and row[column] not in known:
                raise ValueError(f'{source} 第 {index} 行: {column} 引用不存在')
    return tables


def main():
    parser = argparse.ArgumentParser(description='检查交换资料能否正常读入')
    parser.add_argument('directory', type=Path, nargs='?', default=Path('data'))
    directory = parser.parse_args().directory
    try:
        tables = load_sources(directory)
    except (OSError, ValueError) as error:
        parser.exit(1, f'{error}\n')
    print(json.dumps({name: len(items) for name, items in tables.items()}, ensure_ascii=False))


if __name__ == '__main__':
    main()
