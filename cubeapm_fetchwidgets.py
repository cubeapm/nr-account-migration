#!/usr/bin/env python3
"""
Optimized Dashboard Widget Fetcher
Eliminates redundancy by using already-fetched dashboard data
"""

import os
import sys
import argparse
import library.localstore as store
import library.migrationlogger as logger
import library.clients.entityclient as ec
import library.utils as utils
from pathlib import Path


logger = logger.get_logger(os.path.basename(__file__))


def migrate(
    src_acct_id: int,
    src_region: str,
    src_api_key: str,
):
    # Load already-fetched dashboard definitions
    try:
        dashboards_data = store.load_json_file(str(src_acct_id), "dashboards", 'dashboards.json')
        logger.info('Loaded %d dashboard definitions from existing file' % len(dashboards_data))
    except FileNotFoundError:
        logger.error('Dashboard definitions file not found. Please run cubeapm_fetchdashboards.py first.')
        return

    all_widgets = {
        'dashboards': [],
        'widgets': []
    }
    
    for dashboard_result in dashboards_data:
        dashboard_name = dashboard_result.get('name', 'Unknown')
        dashboard_guid = dashboard_result.get('guid')
        
        logger.info('Fetching widgets for dashboard: %s' % dashboard_name)
        
        if not dashboard_guid:
            logger.warning('Dashboard GUID not found: %s' % dashboard_name)
            continue
        
        # Get dashboard widgets using the GUID from existing data
        widgets_result = ec.get_dashboard_widgets(src_api_key, dashboard_guid, src_region)
        if not widgets_result or not widgets_result.get('entityFound'):
            logger.warning('Widgets not found for dashboard: %s' % dashboard_name)
            continue
        
        dashboard_widgets = widgets_result['entity']
        
        # Store dashboard info with existing definition
        dashboard_info = {
            'name': dashboard_name,
            'guid': dashboard_guid,
            'definition': dashboard_result,  # Use existing definition
            'widgets': dashboard_widgets
        }
        all_widgets['dashboards'].append(dashboard_info)
        
        # Extract all widgets for processing
        if 'pages' in dashboard_widgets:
            for page in dashboard_widgets['pages']:
                if 'widgets' in page:
                    for widget in page['widgets']:
                        widget['dashboard_name'] = dashboard_name
                        widget['dashboard_guid'] = dashboard_guid
                        all_widgets['widgets'].append(widget)
        
        widget_count = len(dashboard_widgets.get('pages', [{}])[0].get('widgets', [])) if dashboard_widgets.get('pages') else 0
        logger.info('Found %d widgets for dashboard: %s' % (widget_count, dashboard_name))
    
    save_dashboard_widgets(str(src_acct_id), all_widgets)


def save_dashboard_widgets(account_id, all_widgets):
    base_dir = Path("db")
    dashboards_dir = base_dir / account_id / "dashboards"
    dashboards_dir.mkdir(parents=True, exist_ok=True)
    store.save_json(dashboards_dir, "dashboard_widgets.json", all_widgets)
    
    logger.info('Saved %d dashboards with %d total widgets' % (
        len(all_widgets['dashboards']),
        len(all_widgets['widgets'])
    ))


def create_argument_parser():
    parser = argparse.ArgumentParser(
        description='Fetch dashboard widgets from New Relic (Optimized - uses existing dashboard data)'
    )
    return configure_parser(parser)


def configure_parser(
    parser: argparse.ArgumentParser,
    is_standalone: bool = True
):
    parser.add_argument(
        '--sourceAccount',
        '--source_account_id',
        nargs=1,
        type=int, 
        required=is_standalone,
        help='Source accountId',
        dest='source_account_id'
    )
    parser.add_argument(
        '--sourceRegion',
        '--source_region',
        nargs=1,
        type=str,
        required=False,
        help='Source Account Region us(default) or eu',
        dest='source_region'
    )
    parser.add_argument(
        '--sourceApiKey',
        '--source_api_key',
        nargs=1,
        type=str,
        required=False,
        help='Source account API Key or set environment variable ENV_SOURCE_API_KEY',
        dest='source_api_key'
    )
    return parser


def main():
    parser = create_argument_parser()
    args = parser.parse_args()
    source_api_key = utils.ensure_source_api_key(args)
    if not source_api_key:
        utils.error_and_exit('source_api_key', 'ENV_SOURCE_API_KEY')
    sourceRegion = utils.ensure_source_region(args)

    migrate(
        args.source_account_id[0],
        sourceRegion,
        source_api_key
    )


if __name__ == '__main__':
    main() 