#!/usr/bin/env python3
"""
JSON to PostgreSQL Migration Script
Converts dashboard export JSON (same shape as json_to_mysql_migration.py) to PostgreSQL INSERT statements.
"""

import argparse
import json
from datetime import datetime


def escape_sql_string(s):
    """Escape for PostgreSQL string literals (double single-quotes)."""
    if s is None:
        return 'NULL'
    return "'" + str(s).replace("\\", "\\\\").replace("'", "''") + "'"


def escape_json_for_sql(json_obj):
    """JSON value as a quoted literal, cast to jsonb (adjust to ::json if your columns are json)."""
    if json_obj is None:
        return 'NULL'
    json_str = json.dumps(json_obj, separators=(',', ':'))
    return "'" + json_str.replace("\\", "\\\\").replace("'", "''") + "'::jsonb"


def to_react_grid_layout(layout_dict, item_id):
    """Convert input layout with keys {column,row,width,height} to React Grid Layout {x,y,w,h,i}"""
    if not isinstance(layout_dict, dict):
        return None
    column = layout_dict.get('column')
    row = layout_dict.get('row')
    width = layout_dict.get('width')
    height = layout_dict.get('height')
    if column is None or row is None or width is None or height is None:
        return None
    try:
        x = max(int(column) - 1, 0)
        y = max(int(row) - 1, 0)
        w = int(width)
        h = int(height)
    except Exception:
        return None
    return {'x': x, 'y': y, 'w': w, 'h': h, 'i': str(item_id), 'moved': False, 'static': False}


def generate_postgresql_inserts(json_file_path, output_file_path):
    """Generate PostgreSQL INSERT statements from JSON file."""

    with open(json_file_path, 'r', encoding='utf-8') as f:
        raw_data = json.load(f)

    if isinstance(raw_data, dict) and 'dashboards' in raw_data:
        dashboards_data = raw_data.get('dashboards', [])
    elif isinstance(raw_data, list):
        dashboards_data = raw_data
    else:
        raise ValueError('Unsupported dashboard JSON structure')

    current_timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    with open(output_file_path, 'w', encoding='utf-8') as f:
        f.write("-- PostgreSQL Migration Script for CubeAPM Dashboards\n")
        f.write(f"-- Generated on: {current_timestamp}\n\n")

        f.write("BEGIN;\n\n")

        f.write("-- Clear existing data (comment out if not needed)\n")
        f.write("DELETE FROM panels;\n")
        f.write("DELETE FROM dashboards;\n\n")

        f.write("-- Insert dashboards\n")
        dashboard_inserts = []
        dashboard_id_mapping = {}

        for i, dashboard in enumerate(dashboards_data, start=1):
            new_dashboard_id = i
            dashboard_id_mapping[i] = new_dashboard_id

            title = dashboard.get('title') or dashboard.get('name', '')
            variables = dashboard.get('variables') or dashboard.get('templateVariables') or []

            dashboard_sql = (
                f"INSERT INTO dashboards (id, title, variables, status, created_at, updated_at) "
                f"VALUES ({new_dashboard_id}, {escape_sql_string(title)}, "
                f"{escape_json_for_sql(variables)}, 'ACTIVE', "
                f"'{current_timestamp}'::timestamp, '{current_timestamp}'::timestamp);"
            )
            dashboard_inserts.append(dashboard_sql)

        for sql in dashboard_inserts:
            f.write(sql + "\n")
        f.write("\n")

        f.write("-- Insert panels\n")
        panel_inserts = []
        panel_id_counter = 1

        for index, dashboard in enumerate(dashboards_data, start=1):
            new_dashboard_id = dashboard_id_mapping[index]
            pages = dashboard.get('pages', [])

            for page in pages:
                page_name = page.get('name') or page.get('title') or ''
                widgets = page.get('widgets', [])

                for widget in widgets:
                    panel_type_raw = widget.get('type', '')
                    if panel_type_raw != 'ignore':
                        type_map = {
                            'linechart': 'linechart',
                            'linechart_area': 'linechart',
                            'table': 'table',
                            'chart': 'linechart',
                            'scorecard': 'scorecard',
                            'ignore': 'ignore',
                        }
                        stack = False
                        panel_type = type_map.get(panel_type_raw, panel_type_raw)
                        if panel_type_raw == 'linechart_area':
                            stack = True
                        layout = widget.get('layout', {})
                        rgl_layout = to_react_grid_layout(layout, panel_id_counter)
                        raw_title = widget.get('title')
                        title = raw_title if isinstance(raw_title, str) and raw_title.strip() != '' else 'title not found'
                        queries_in = widget.get('queries') or []
                        queries_objects = []
                        for q in queries_in if isinstance(queries_in, list) else []:
                            if isinstance(q, dict):
                                queries_objects.append({
                                    'unit': q.get('unit', 'number'),
                                    'query': q.get('query', ''),
                                    'title': q.get('title', ''),
                                })
                        query_top = queries_objects[0]['query'] if len(queries_objects) > 0 else ''
                        config = {
                            'page': page_name,
                            'unit': 'number',
                            'query': query_top,
                            'stack': stack,
                            'legend': {'pos': 'right', 'label': [], 'formula': 'avg'},
                            'queries': queries_objects,
                            'showSearch': False,
                            'defaultSortCol': 0,
                        }

                        layout_val = rgl_layout if rgl_layout is not None else layout
                        panel_sql = (
                            f"INSERT INTO panels (id, dashboard_id, type, layout, title, status, config, created_at, updated_at) "
                            f"VALUES ({panel_id_counter}, {new_dashboard_id}, {escape_sql_string(panel_type)}, "
                            f"{escape_json_for_sql(layout_val)}, {escape_sql_string(title)}, 'ACTIVE', "
                            f"{escape_json_for_sql(config)}, '{current_timestamp}'::timestamp, '{current_timestamp}'::timestamp);"
                        )
                        panel_inserts.append(panel_sql)
                        panel_id_counter += 1

        for sql in panel_inserts:
            f.write(sql + "\n")
        f.write("\n")

        f.write(
            "-- Sync SERIAL / GENERATED IDENTITY after explicit ids "
            "(skip or adjust if id is not backed by a sequence)\n"
        )
        f.write(
            "SELECT setval(pg_get_serial_sequence('dashboards', 'id'), "
            "COALESCE((SELECT MAX(id) FROM dashboards), 1), true);\n"
        )
        f.write(
            "SELECT setval(pg_get_serial_sequence('panels', 'id'), "
            "COALESCE((SELECT MAX(id) FROM panels), 1), true);\n\n"
        )

        f.write("COMMIT;\n\n")

        f.write("-- Migration Summary:\n")
        f.write(f"-- Dashboards inserted: {len(dashboards_data)}\n")
        f.write(f"-- Panels inserted: {len(panel_inserts)}\n")
        f.write("\n-- ID Mapping:\n")
        f.write("-- Dashboard IDs: 1..N by input order; panel IDs sequential from 1\n")

    print(f"Migration script generated: {output_file_path}")
    print(f"Dashboards: {len(dashboards_data)}")
    print(f"Panels: {len(panel_inserts)}")


def main():
    parser = argparse.ArgumentParser(
        description='Convert JSON dashboard data to PostgreSQL INSERT statements'
    )
    parser.add_argument(
        '--input', '-i',
        default='output/6724035_cubeapm_dashboards.json',
        help='Input JSON file path (default: output/6724035_cubeapm_dashboards.json)',
    )
    parser.add_argument(
        '--output', '-o',
        default='postgresql_migration.sql',
        help='Output SQL file path (default: postgresql_migration.sql)',
    )

    args = parser.parse_args()

    try:
        generate_postgresql_inserts(args.input, args.output)
        print(f"Successfully generated PostgreSQL migration script: {args.output}")
    except FileNotFoundError:
        print(f"Error: Input file '{args.input}' not found.")
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in input file: {e}")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == '__main__':
    main()
