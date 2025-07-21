#!/usr/bin/env python3

import json
import sys

def debug_widgets():
    try:
        # Load the dashboard widgets file
        with open('db/123456/dashboards/dashboard_widgets.json', 'r') as f:
            data = json.load(f)
        
        print("Dashboard widgets structure:")
        print(json.dumps(data, indent=2))
        
        # Look at first widget structure
        if 'widgets' in data and data['widgets']:
            first_widget = data['widgets'][0]
            print("\nFirst widget structure:")
            print(json.dumps(first_widget, indent=2))
            
            # Check for NRQL queries
            if 'rawConfiguration' in first_widget:
                raw_config = first_widget['rawConfiguration']
                print("\nRaw configuration keys:")
                print(list(raw_config.keys()))
                
                if 'nrqlQueries' in raw_config:
                    print("\nNRQL queries found:")
                    for i, query in enumerate(raw_config['nrqlQueries']):
                        print(f"Query {i+1}: {query}")
        
    except FileNotFoundError:
        print("dashboard_widgets.json not found. Please run the fetch steps first.")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == '__main__':
    debug_widgets() 