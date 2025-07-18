#!/usr/bin/env python3
"""
Convert CubeAPM dashboard JSON data to SQLite3 INSERT statements
"""

import json
import argparse
import sqlite3
from pathlib import Path


def create_sqlite_tables(cursor):
    """
    Create the necessary tables in SQLite3
    """
    # Create dashboards table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS dashboards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            variables TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
    ''')
    
    # Create panels table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS panels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dashboard_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            layout TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            config TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            FOREIGN KEY (dashboard_id) REFERENCES dashboards (id) ON DELETE CASCADE
        )
    ''')
    
    # Create indexes
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_panels_dashboard_id ON panels (dashboard_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_panels_type ON panels (type)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_dashboards_title ON dashboards (title)')


def insert_dashboard_data(cursor, dashboards_data):
    """
    Insert dashboard data into SQLite3
    """
    dashboard_inserts = []
    panel_inserts = []
    
    for i, dashboard in enumerate(dashboards_data):
        # Use enumeration index to ensure unique IDs
        dashboard_id = i
        title = dashboard.get('title', 'Unknown Dashboard')
        variables = json.dumps(dashboard.get('variables', []))
        
        dashboard_inserts.append((
            dashboard_id,
            title,
            variables,
            'ACTIVE'  # default status - uppercase to match application
        ))
        
        # Insert panels for this dashboard
        panels = dashboard.get('panels', [])
        for panel in panels:
            panel_id = panel.get('id', 0)
            panel_type = panel.get('type', 'linechart')
            layout = json.dumps(panel.get('layout', {}))
            panel_title = panel.get('title', 'Unknown Panel')
            config = json.dumps(panel.get('config', {}))
            
            panel_inserts.append((
                panel_id,
                dashboard_id,
                panel_type,
                layout,
                panel_title,
                'ACTIVE',  # default status - uppercase to match application
                config
            ))
    
    # Execute dashboard inserts
    cursor.executemany('''
        INSERT OR REPLACE INTO dashboards (id, title, variables, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, datetime('now', '+05:30'), datetime('now', '+05:30'))
    ''', dashboard_inserts)
    
    # Execute panel inserts
    cursor.executemany('''
        INSERT OR REPLACE INTO panels (id, dashboard_id, type, layout, title, status, config, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', '+05:30'), datetime('now', '+05:30'))
    ''', panel_inserts)
    
    return len(dashboard_inserts), len(panel_inserts)


def generate_sql_inserts(dashboards_data):
    """
    Generate SQL INSERT statements as strings
    """
    sql_statements = []
    
    # Add table creation statements
    sql_statements.append("-- Create dashboards table")
    sql_statements.append("""
CREATE TABLE IF NOT EXISTS dashboards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    variables TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);
""")
    
    sql_statements.append("-- Create panels table")
    sql_statements.append("""
CREATE TABLE IF NOT EXISTS panels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dashboard_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    layout TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    config TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    FOREIGN KEY (dashboard_id) REFERENCES dashboards (id) ON DELETE CASCADE
);
""")
    
    sql_statements.append("-- Create indexes")
    sql_statements.append("CREATE INDEX IF NOT EXISTS idx_panels_dashboard_id ON panels (dashboard_id);")
    sql_statements.append("CREATE INDEX IF NOT EXISTS idx_panels_type ON panels (type);")
    sql_statements.append("CREATE INDEX IF NOT EXISTS idx_dashboards_title ON dashboards (title);")
    sql_statements.append("")
    
    # Generate INSERT statements
    for i, dashboard in enumerate(dashboards_data):
        # Use enumeration index to ensure unique IDs
        dashboard_id = i
        title = (dashboard.get('title') or 'Unknown Dashboard').replace("'", "''")  # Escape single quotes
        variables = json.dumps(dashboard.get('variables', [])).replace("'", "''")
        
        sql_statements.append(f"-- Insert dashboard: {title}")
        sql_statements.append(f"INSERT OR REPLACE INTO dashboards (id, title, variables, status, created_at, updated_at) VALUES ({dashboard_id}, '{title}', '{variables}', 'ACTIVE', datetime('now', '+05:30'), datetime('now', '+05:30'));")
        
        # Insert panels for this dashboard
        panels = dashboard.get('panels', [])
        for panel in panels:
            panel_id = panel.get('id', 0)
            panel_type = (panel.get('type') or 'linechart').replace("'", "''")
            layout = json.dumps(panel.get('layout', {})).replace("'", "''")
            panel_title = (panel.get('title') or 'Unknown Panel').replace("'", "''")
            config = json.dumps(panel.get('config', {})).replace("'", "''")
            
            sql_statements.append(f"INSERT OR REPLACE INTO panels (id, dashboard_id, type, layout, title, status, config, created_at, updated_at) VALUES ({panel_id}, {dashboard_id}, '{panel_type}', '{layout}', '{panel_title}', 'ACTIVE', '{config}', datetime('now', '+05:30'), datetime('now', '+05:30'));")
        
        sql_statements.append("")
    
    return sql_statements


def main():
    parser = argparse.ArgumentParser(description='Convert CubeAPM dashboard JSON to SQLite3')
    parser.add_argument('--input', '-i', type=str, required=True,
                       help='Input JSON file path')
    parser.add_argument('--output', '-o', type=str, default='output.sql',
                       help='Output SQL file path (default: output.sql)')
    parser.add_argument('--database', '-d', type=str, default=None,
                       help='SQLite3 database file path (optional)')
    
    args = parser.parse_args()
    
    # Read JSON data
    print(f"Reading JSON data from {args.input}...")
    with open(args.input, 'r') as f:
        dashboards_data = json.load(f)
    
    print(f"Found {len(dashboards_data)} dashboards")
    
    # Generate SQL statements
    print("Generating SQL INSERT statements...")
    sql_statements = generate_sql_inserts(dashboards_data)
    
    # Write SQL file
    with open(args.output, 'w') as f:
        f.write('\n'.join(sql_statements))
    
    print(f"SQL statements written to {args.output}")
    
    # Optionally create SQLite3 database
    if args.database:
        print(f"Creating SQLite3 database at {args.database}...")
        conn = sqlite3.connect(args.database)
        cursor = conn.cursor()
        
        # Create tables
        create_sqlite_tables(cursor)
        
        # Insert data
        dashboard_count, panel_count = insert_dashboard_data(cursor, dashboards_data)
        
        # Commit and close
        conn.commit()
        conn.close()
        
        print(f"Database created successfully!")
        print(f"Inserted {dashboard_count} dashboards and {panel_count} panels")
        print(f"Run: sqlite3 {args.database} < {args.output}")


if __name__ == '__main__':
    main() 