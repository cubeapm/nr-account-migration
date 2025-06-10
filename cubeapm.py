import os
import sys
import argparse
import library.localstore as store
import library.migrationlogger as logger
import library.clients.alertsclient as ac
import library.utils as utils
from pathlib import Path


logger = logger.get_logger(os.path.basename(__file__))


def migrate(
    policy_file_path: str,
    src_acct_id: int,
    src_region: str,
    src_api_key: str,
):
    policy_names = store.load_names(policy_file_path)

    all_alert_conditions = {
        'nrql': [],
        'app': [],
        'infra': [],
        'synthetic': [],
    }
    for policy_name in policy_names:
        src_result = ac.get_policy(src_api_key, policy_name, src_region)
        if not src_result['policyFound']:
            continue
        src_policy = src_result['policy']

        logger.info('Fetching conditions for policy id %s' % str(src_policy['id']))

        # logger.info('Loading source NRQL conditions ')
        nrql_conds = ac.get_nrql_conditions(src_api_key, src_acct_id, src_policy['id'], src_region)[ac.CONDITIONS]
        # logger.info('Fetched %d source conditions' % len(nrql_conds))
        if nrql_conds:
            all_alert_conditions['nrql'].extend(nrql_conds)

        # logger.info('loading source app conditions')
        app_conditions = ac.get_app_conditions(src_api_key, src_policy['id'], src_region)[ac.CONDITIONS]
        # logger.info("Found app alert conditions " + str(len(app_conditions)))
        if app_conditions:
            all_alert_conditions['app'].extend(app_conditions)

        # logger.info('Loading source infrastructure conditions ')
        infra_conditions = ac.get_infra_conditions(src_api_key, src_policy['id'], src_region)[ac.INFRA_CONDITIONS]
        # logger.info('Found infrastructure conditions ' + str(len(infra_conditions)))
        if infra_conditions:
            all_alert_conditions['infra'].extend(infra_conditions)

        # logger.info('Loading source synthetic conditions ')
        synthetic_conditions = ac.get_synthetic_conditions(src_api_key, src_policy['id'], src_region)[ac.SYNTH_CONDITIONS]
        # logger.info('Found synthetic conditions ' + str(len(synthetic_conditions)))
        if synthetic_conditions:
            all_alert_conditions['synthetic'].extend(synthetic_conditions)
    
    save_alert_conditions(str(src_acct_id), all_alert_conditions)


def save_alert_conditions(account_id, all_alert_conditions):
    base_dir = Path("db")
    alert_policies_dir = base_dir / account_id / store.ALERT_POLICIES_DIR
    store.save_json(alert_policies_dir, "alert_conditions.json", all_alert_conditions)


def create_argument_parser():
    parser = argparse.ArgumentParser(
        description='Migrate Alert Policies, Alert Conditions, and channels'
    )
    return configure_parser(parser)


def configure_parser(
    parser: argparse.ArgumentParser,
    is_standalone: bool = True
):
    parser.add_argument(
        '--fromFile',
        '--policy_file',
        nargs=1,
        type=str,
        required=True,
        help='Path to file with alert policy names',
        dest='policy_file'
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
    policy_file = args.policy_file[0] if args.policy_file else None
    if not policy_file:
        logger.error('A policy file must be specified.')
        sys.exit()
    sourceRegion = utils.ensure_source_region(args)

    migrate(
        policy_file,
        args.source_account_id[0],
        sourceRegion,
        source_api_key
    )

if __name__ == '__main__':
    main()
