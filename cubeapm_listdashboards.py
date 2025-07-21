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
    logger.info('Fetching all dashboards for account: %s' % src_acct_id)
    
    # Get all dashboard entities
    dashboard_entities = ec.gql_get_entities_by_type(src_api_key, ec.DASHBOARD, src_acct_id, None, None, src_region)
    
    if not dashboard_entities or 'entities' not in dashboard_entities:
        logger.error('No dashboard entities found')
        return
    
    dashboard_names = []
    for entity in dashboard_entities['entities']:
        dashboard_name = entity.get('name', 'Unknown')
        dashboard_names.append(dashboard_name)
        logger.info('Found dashboard: %s (GUID: %s)' % (dashboard_name, entity.get('guid', 'N/A')))
    
    # Save dashboard names to file
    save_dashboard_names(dashboard_names)
    
    logger.info('Found %d dashboards total' % len(dashboard_names))


def save_dashboard_names(dashboard_names):
    # Create dashboard_names.txt file
    with open('dashboard_names.txt', 'w') as f:
        for name in dashboard_names:
            f.write(name + '\n')
    
    logger.info('Saved dashboard names to dashboard_names.txt')


def create_argument_parser():
    parser = argparse.ArgumentParser(
        description='List all dashboards from New Relic'
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