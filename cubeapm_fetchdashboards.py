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
    dashboard_file_path: str,
    src_acct_id: int,
    src_region: str,
    src_api_key: str,
):
    dashboard_names = store.load_names(dashboard_file_path)

    all_dashboards = []
    for dashboard_name in dashboard_names:
        logger.info('Fetching dashboard: %s' % dashboard_name)
        
        # Get dashboard definition
        dashboard_result = ec.get_dashboard_definition(src_api_key, dashboard_name, src_acct_id, src_region)
        if not dashboard_result:
            logger.warning('Dashboard not found: %s' % dashboard_name)
            continue
            
        all_dashboards.append(dashboard_result)
        logger.info('Found dashboard: %s (GUID: %s)' % (dashboard_name, dashboard_result.get('guid', 'N/A')))
    
    save_dashboards(str(src_acct_id), all_dashboards)
    save_dashboard_csv(str(src_acct_id), all_dashboards)


def save_dashboards(account_id, all_dashboards):
    base_dir = Path("db")
    dashboards_dir = base_dir / account_id / "dashboards"
    dashboards_dir.mkdir(parents=True, exist_ok=True)
    store.save_json(dashboards_dir, "dashboards.json", all_dashboards)


def save_dashboard_csv(account_id, all_dashboards):
    csv_lines = []
    for dashboard in all_dashboards:
        csv_lines.append(dashboard.get('name', 'Unknown'))
    
    csv_file = store.create_output_file('%s_dashboards.csv' % account_id)
    with csv_file.open('w') as f:
        for line in csv_lines:
            f.write(line + '\n')
    
    logger.info('Saved %d dashboard names to CSV' % len(csv_lines))


def create_argument_parser():
    parser = argparse.ArgumentParser(
        description='Fetch dashboard definitions from New Relic'
    )
    return configure_parser(parser)


def configure_parser(
    parser: argparse.ArgumentParser,
    is_standalone: bool = True
):
    parser.add_argument(
        '--fromFile',
        '--dashboard_file',
        nargs=1,
        type=str,
        required=True,
        help='Path to file with dashboard names',
        dest='dashboard_file'
    )
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
    dashboard_file = args.dashboard_file[0] if args.dashboard_file else None
    if not dashboard_file:
        logger.error('A dashboard file must be specified.')
        sys.exit()
    sourceRegion = utils.ensure_source_region(args)

    migrate(
        dashboard_file,
        args.source_account_id[0],
        sourceRegion,
        source_api_key
    )


if __name__ == '__main__':
    main() 