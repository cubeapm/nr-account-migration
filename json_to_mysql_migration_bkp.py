#!/usr/bin/env python3
"""
JSON to MySQL Migration Script
Converts 6724035_cubeapm_dashboards.json to MySQL INSERT statements
"""

import json
import re
from datetime import datetime
import argparse
from typing import Any, Dict, List

def escape_sql_string(s):
    """Escape single quotes and backslashes for SQL strings"""
    if s is None:
        return 'NULL'
    return "'" + str(s).replace("\\", "\\\\").replace("'", "''") + "'"

def escape_json_for_sql(json_obj):
    """Convert JSON object to properly escaped SQL JSON string"""
    if json_obj is None:
        return 'NULL'
    json_str = json.dumps(json_obj, separators=(',', ':'))
    return "'" + json_str.replace("\\", "\\\\").replace("'", "''") + "'"

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
    # RGL expects 0-based x,y. Source appears 1-based.
    try:
        x = max(int(column) - 1, 0)
        y = max(int(row) - 1, 0)
        w = int(width)
        h = int(height)
    except Exception:
        return None
    return {
        'x': x,
        'y': y,
        'w': w,
        'h': h,
        'i': str(item_id),
        'moved': False,
        'static': False,
    }

def normalize_widget_for_frontend(widget: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of widget with safe defaults so the frontend never sees undefined values.
    - Ensure queries is always a list
    - Ensure each query has a string 'query' field (default to '')
    """
    if not isinstance(widget, dict):
        return {}
    normalized: Dict[str, Any] = dict(widget)
    # Coerce common string fields
    if not isinstance(normalized.get('title'), str):
        normalized['title'] = ''
    if not isinstance(normalized.get('content'), str):
        normalized['content'] = ''
    queries: Any = normalized.get('queries')
    safe_queries: List[Dict[str, Any]] = []
    if isinstance(queries, list):
        for q in queries:
            if isinstance(q, dict):
                q_copy = dict(q)
                if not isinstance(q_copy.get('query'), str):
                    q_copy['query'] = ''
                safe_queries.append(q_copy)
    normalized['queries'] = safe_queries
    # Also expose a flat array of strings and a single query for convenience
    queries_flat: List[str] = [q.get('query', '') for q in safe_queries]
    normalized['queriesFlat'] = queries_flat
    normalized['query'] = queries_flat[0] if len(queries_flat) > 0 else ''
    return normalized

def generate_mysql_inserts(json_file_path, output_file_path):
    """Generate MySQL INSERT statements from JSON file"""
    
    # Read JSON file
    with open(json_file_path, 'r', encoding='utf-8') as f:
        raw_data = json.load(f)
    
    # Normalize dashboards list based on detected schema
    if isinstance(raw_data, dict) and 'dashboards' in raw_data:
        dashboards_data = raw_data.get('dashboards', [])
    elif isinstance(raw_data, list):
        dashboards_data = raw_data
    else:
        raise ValueError('Unsupported dashboard JSON structure')
    
    # Current timestamp for created_at and updated_at
    current_timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # Open output file
    with open(output_file_path, 'w', encoding='utf-8') as f:
        f.write("-- MySQL Migration Script for CubeAPM Dashboards\n")
        f.write(f"-- Generated on: {current_timestamp}\n")
        f.write("-- Source: 6724035_cubeapm_dashboards.json\n\n")
        
        # Start transaction
        f.write("START TRANSACTION;\n\n")
        
        # Clear existing data (optional - comment out if not needed)
        f.write("-- Clear existing data\n")
        f.write("DELETE FROM panels;\n")
        f.write("DELETE FROM dashboards;\n\n")
        
        # Reset auto-increment counters
        f.write("-- Reset auto-increment counters\n")
        f.write("ALTER TABLE panels AUTO_INCREMENT = 1;\n")
        f.write("ALTER TABLE dashboards AUTO_INCREMENT = 1;\n\n")
        
        # Insert dashboards with unique sequential IDs
        f.write("-- Insert dashboards\n")
        dashboard_inserts = []
        dashboard_id_mapping = {}  # Map synthetic original key to new sequential ID
        
        for i, dashboard in enumerate(dashboards_data, start=1):
            # The input does not have a stable numeric id; synthesize a key from index
            synthetic_original_id = i  # used only for mapping within this run
            new_dashboard_id = i
            dashboard_id_mapping[synthetic_original_id] = new_dashboard_id
            
            # Title may be under 'title' or 'name'
            title = dashboard.get('title') or dashboard.get('name', '')
            # Variables may be absent; try common alternatives
            variables = dashboard.get('variables') or dashboard.get('templateVariables') or []
            
            # Create dashboard insert statement
            dashboard_sql = (
                f"INSERT INTO dashboards (id, title, variables, status, created_at, updated_at) "
                f"VALUES ({new_dashboard_id}, {escape_sql_string(title)}, "
                f"{escape_json_for_sql(variables)}, 'ACTIVE', "
                f"'{current_timestamp}', '{current_timestamp}');"
            )
            dashboard_inserts.append(dashboard_sql)
        
        # Write dashboard inserts
        for sql in dashboard_inserts:
            f.write(sql + "\n")
        f.write("\n")
        
        # Insert panels with unique sequential IDs
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
                    panel_type = widget.get('type', '')
                    layout = widget.get('layout', {})
                    rgl_layout = to_react_grid_layout(layout, panel_id_counter)
                    title = widget.get('title') or None
                    # Store remaining widget fields plus page context as config
                    normalized_widget = normalize_widget_for_frontend(widget)
                    query_strings = []
                    queries_objects = []
                    for q in normalized_widget.get('queries', []):
                        if isinstance(q, dict):
                            qs = q.get('query')
                            query_strings.append(qs if isinstance(qs, str) else '')
                            # preserve object with common UI fields
                            queries_objects.append({
                                'unit': q.get('unit', 'number'),
                                'model': q.get('model', {}),
                                'query': q.get('query', ''),
                                'title': q.get('title', ''),
                            })
                    # Safe string helpers commonly used by frontend
                    title_str = title if isinstance(title, str) else ''
                    content_str = normalized_widget.get('content', '') if isinstance(normalized_widget.get('content'), str) else ''
                    config = {
                        'page': page_name,
                        # expose both the full widget and a flat list of NRQL strings
                        'widget': normalized_widget,
                        'query': query_strings[0] if len(query_strings) > 0 else '',
                        'queries': queries_objects,
                        'titleStr': title_str,
                        'contentStr': content_str,
                        'queryStr': normalized_widget.get('query', ''),
                        'queriesStr': query_strings,
                        'legend': {'pos': 'right', 'label': [], 'formula': 'avg'},
                        'unit': 'number',
                        'stack': False,
                        'showSearch': False,
                        'defaultSortCol': 0,
                    }
                    
                    # Create panel insert statement with unique sequential ID
                    panel_sql = (
                        f"INSERT INTO panels (id, dashboard_id, type, layout, title, status, config, created_at, updated_at) "
                    f"VALUES ({panel_id_counter}, {new_dashboard_id}, {escape_sql_string(panel_type)}, "
                    f"{escape_json_for_sql(rgl_layout if rgl_layout is not None else layout)}, {escape_sql_string(title)}, 'ACTIVE', "
                        f"{escape_json_for_sql(config)}, '{current_timestamp}', '{current_timestamp}');"
                    )
                    panel_inserts.append(panel_sql)
                    panel_id_counter += 1
        
        # Write panel inserts
        for sql in panel_inserts:
            f.write(sql + "\n")
        f.write("\n")
        
        # Commit transaction
        f.write("COMMIT;\n\n")
        
        # Summary
        f.write(f"-- Migration Summary:\n")
        f.write(f"-- Dashboards inserted: {len(dashboards_data)}\n")
        f.write(f"-- Panels inserted: {len(panel_inserts)}\n")
        f.write(f"-- Total statements: {len(dashboard_inserts) + len(panel_inserts) + 6}\n")  # +6 for transaction, deletes, and resets
        
        # ID mapping information
        f.write(f"\n-- ID Mapping Information:\n")
        f.write(f"-- Dashboard IDs assigned sequentially (1, 2, 3, ...) based on input order\n")
        f.write(f"-- Panel IDs were assigned sequential values starting from 1\n")
        f.write(f"-- Dashboard-Panel relationships preserved using new dashboard IDs\n")
    
    print(f"Migration script generated: {output_file_path}")
    print(f"Dashboards: {len(dashboards_data)}")
    print(f"Panels: {len(panel_inserts)}")

def main():
    parser = argparse.ArgumentParser(description='Convert JSON dashboard data to MySQL INSERT statements')
    parser.add_argument('--input', '-i', default='output/6724035_cubeapm_dashboards.json',
                       help='Input JSON file path (default: output/6724035_cubeapm_dashboards.json)')
    parser.add_argument('--output', '-o', default='mysql_migration.sql',
                       help='Output SQL file path (default: mysql_migration.sql)')
    
    args = parser.parse_args()
    
    try:
        generate_mysql_inserts(args.input, args.output)
        print(f"Successfully generated MySQL migration script: {args.output}")
    except FileNotFoundError:
        print(f"Error: Input file '{args.input}' not found.")
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in input file: {e}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main() 