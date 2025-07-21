#!/usr/bin/env python3
"""
JSON to MySQL Migration Script
Converts 6724035_cubeapm_dashboards.json to MySQL INSERT statements
"""

import json
import re
from datetime import datetime
import argparse

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

def generate_mysql_inserts(json_file_path, output_file_path):
    """Generate MySQL INSERT statements from JSON file"""
    
    # Read JSON file
    with open(json_file_path, 'r', encoding='utf-8') as f:
        dashboards_data = json.load(f)
    
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
        dashboard_id_mapping = {}  # Map original ID to new sequential ID
        
        for i, dashboard in enumerate(dashboards_data, start=1):
            original_id = dashboard.get('id', 0)
            new_dashboard_id = i
            dashboard_id_mapping[original_id] = new_dashboard_id
            
            title = dashboard.get('title', '')
            variables = dashboard.get('variables', [])
            
            # Create dashboard insert statement
            dashboard_sql = (
                f"INSERT INTO dashboards (id, title, variables, status, created_at, updated_at) "
                f"VALUES ({new_dashboard_id}, {escape_sql_string(title)}, "
                f"{escape_json_for_sql(variables)}, 'active', "
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
        
        for dashboard in dashboards_data:
            original_dashboard_id = dashboard.get('id', 0)
            new_dashboard_id = dashboard_id_mapping[original_dashboard_id]
            panels = dashboard.get('panels', [])
            
            for panel in panels:
                panel_type = panel.get('type', '')
                layout = panel.get('layout', {})
                title = panel.get('title')
                config = panel.get('config', {})
                
                # Create panel insert statement with unique sequential ID
                panel_sql = (
                    f"INSERT INTO panels (id, dashboard_id, type, layout, title, status, config, created_at, updated_at) "
                    f"VALUES ({panel_id_counter}, {new_dashboard_id}, {escape_sql_string(panel_type)}, "
                    f"{escape_json_for_sql(layout)}, {escape_sql_string(title)}, 'active', "
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
        f.write(f"-- Original dashboard IDs were remapped to sequential IDs (1, 2, 3, ...)\n")
        f.write(f"-- Panel IDs were assigned sequential values starting from 1\n")
        f.write(f"-- Dashboard-Panel relationships preserved using new dashboard IDs\n")
    
    print(f"Migration script generated: {output_file_path}")
    print(f"Dashboards: {len(dashboards_data)}")
    print(f"Panels: {len(panel_inserts)}")
    print(f"Dashboard ID mapping: {dashboard_id_mapping}")

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