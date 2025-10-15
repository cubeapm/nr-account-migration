import os
import re
import json
import argparse
import library.localstore as store
import library.migrationlogger as migrationlogger
from pathlib import Path

logger = migrationlogger.get_logger(os.path.basename(__file__))


mapOperator = {
    'ABOVE': '>',
    'ABOVE_OR_EQUALS': '>=',
    'BELOW': '<',
    'BELOW_OR_EQUALS': '<=',
    'EQUALS': '==',
}

idRegEx = r"\(?(?P<idType>appId|appName|entity\.guid)\s*(?:=\s*(?P<guid>\d+|(?:'|\")[^'\"]+(?:'|\"))|IN\s*\((?P<guids>[^)]+)\))\)?"

facetOptionalRegEx = r"(?:FACET\s*(?P<facet>appId|appName|entity\.guid|entity\.name))?"

transactionTypeOptionalRegEx = r"(?:(?:AND\s*)?\(?\s*transactionType\s*=\s*(?:'|\")(?P<transactionType>\w+)(?:'|\")\s*\)?\s*)?"


def resolveEntityGuids(idType, entry, entries_str, entities):
    entries = [entry] if entry else []
    if entries_str:
        entries = entries_str.split(',')
    entries = [x.strip().strip("'\"") for x in entries]
    if not entries:
        raise ValueError("empty entries")
    
    entityType = None
    names = []
    if idType == 'appName':
        entityType = 'APPLICATION'
        names = [{'service': x} for x in entries]
    elif idType == 'appId':
        entityType = 'APPLICATION'
        for e in entries:
            val = int(e)
            _filteredEntities = [x for x in entities if x['entityType'] == 'APM_APPLICATION_ENTITY' and x['applicationId'] == val]
            names.append({'service': _filteredEntities[0]['name']})
    elif idType == 'entity.guid':
        for guid in entries:
            _filteredEntities = [x for x in entities if x['guid'] == guid]
            if not _filteredEntities:
                raise ValueError("entity not found for guid %s" % guid)
            entity = _filteredEntities[0]
            if not entityType:
                entityType = entity['type']
            elif entity['type'] != entityType:
                raise ValueError("mixed entity type for guid " + guid)
            
            if entityType == 'APPLICATION':
                names.append({'service': entity['name']})
            elif entityType == 'KEY_TRANSACTION':
                names.append({'service': entity['application']['entity']['name'], 'root_name': entity['metricName']})
            else:
                raise ValueError("unhandled entity type " + entityType)
    else:
        raise ValueError("unhandled idType " + idType)
    
    if not names:
        raise ValueError("empty names")
    return entityType, names


def parseFacet(entityType, facet):
    if not facet:
        groupBy = []
    elif facet in ['appId', 'appName']:
        groupBy = ['service']
    elif facet in ['entity.guid', 'entity.name']:
        if entityType == "APPLICATION":
            groupBy = ['service']
        elif entityType == "KEY_TRANSACTION":
            groupBy = ['service', 'root_name']
        else:
            raise ValueError("unhandled entity type " + entityType)
    else:
        raise ValueError("unhandled facet " + facet)
    
    return groupBy


def makeEsr(entityType, names):
    if entityType == "APPLICATION":
        if len(names) == 1:
            fragment = 'service="{}"'.format(dquote(names[0]['service']))
            modelOperator = '='
        else:
            fragment = 'service=~"{}"'.format(requote([x['service'] for x in names]))
            modelOperator = '=~'
        labelPairs = [
            {"label": "service", "operator": modelOperator, "values": [x['service'] for x in names], "options": []},
        ]
    elif entityType == "KEY_TRANSACTION":
        if len(names) == 1:
            fragment = 'service="{}", root_name="{}"'.format(dquote(names[0]['service']), dquote(names[0]['root_name']))
            modelOperator = '='
        else:
            # TODO: this is not entirely correct, as it will do many to many match instead of one to one
            fragment = 'service=~"{}", root_name=~"{}"'.format(requote([x['service'] for x in names]), requote([x['root_name'] for x in names]))
            modelOperator = '=~'
        labelPairs = [
            {"label": "service", "operator": modelOperator, "values": [x['service'] for x in names], "options": []},
            {"label": "root_name", "operator": modelOperator, "values": [x['root_name'] for x in names], "options": []},
        ]
    else:
        raise ValueError("unhandled entity type " + entityType)
    
    return fragment, labelPairs

def mapQuery(query, all_entities):
    # apdex ############################
    res = re.search(
        r"^\s*SELECT\s+apdex\s*\(apm\.service\.apdex\)\s*FROM\s+Metric\s+WHERE\s+" + idRegEx + r"\s*" + facetOptionalRegEx + r"\s*$",
        query, flags=re.IGNORECASE
    )
    if res:
        groupdict = res.groupdict()
        
        try:
            entityType, names = resolveEntityGuids(groupdict.get('idType'), groupdict.get('guid'), groupdict.get('guids'), all_entities)
        except Exception as ex:
            return "ERROR", "%s # %s" % (query, ex), '{}', 1

        if entityType != "APPLICATION":
            raise ValueError("unhandled entity type " + entityType)
            
        fragment, labelPairs = makeEsr(entityType, names)

        spanKind = 'span_kind=~"server|consumer"'

        # https://docs.newrelic.com/docs/apm/new-relic-apm/apdex/apdex-measure-user-satisfaction/#apdex-counts
        # Note: We hard-code apdex_t=0.5 here, which is NewRelic's default value.
        # Note: histogram_share(2.0, ...) will include histogram_share(0.5, ...) as well,
        # so we take only half of histogram_share(0.5, ...).
        newQuery = """0.5 *
(
histogram_share(0.5, sum by (service,vmrange) (increase(cube_apm_latency_bucket{{{fragment}, {spanKind}, status_code!="ERROR"}} default 0)))
+
histogram_share(2.0, sum by (service,vmrange) (increase(cube_apm_latency_bucket{{{fragment}, {spanKind}, status_code!="ERROR"}} default 0)))
)
* sum by (service) (increase(cube_apm_latency_count{{{fragment}, {spanKind}, status_code!="ERROR"}} default 0))
/ sum by (service) (increase(cube_apm_latency_count{{{fragment}, {spanKind}}} default 0))""".format(fragment=fragment, spanKind=spanKind)

        return 'APDEX', newQuery, '{}', 1
    
    # request_count ############################
    res = re.search(
        r"^\s*SELECT\s+(?P<rpm1>rate\s*\()?\s*count\s*\(\s*apm\.(?P<mType>service|key)\.transaction\.duration\s*\)\s*(?P<rpm2>,\s*1\s+minute\s*\))?\s*(?:AS\s*(?:\w+|'[^']*'|\"[^\"]*\"))?\s*FROM\s+Metric\s+WHERE\s+" + idRegEx + r"\s*" + transactionTypeOptionalRegEx + r"\s*" + facetOptionalRegEx + r"\s*$",
        query, flags=re.IGNORECASE
    )
    if res:
        groupdict = res.groupdict()

        try:
            entityType, names = resolveEntityGuids(groupdict.get('idType'), groupdict.get('guid'), groupdict.get('guids'), all_entities)
        except Exception as ex:
            return "ERROR", "%s # %s" % (query, ex), '{}', 1

        if entityType == "APPLICATION":
            if groupdict.get('mType') != 'service':
                raise ValueError("unhandled situation: " + query)
        elif entityType == "KEY_TRANSACTION":
            if groupdict.get('mType') != 'key':
                raise ValueError("unhandled situation: " + query)
        else:
            raise ValueError("unhandled entity type " + entityType)
    
        fragment, labelPairs = makeEsr(entityType, names)

        spanKind = 'span_kind=~"server|consumer"'

        groupBy = parseFacet(entityType, groupdict.get('facet'))
        groupByStr = ' by ({})'.format(','.join(groupBy)) if groupBy else ''
        
        model = {
            "model": {
                "type": "quick",
                "calculate": "rpm",
                "value": "0",
                "labelPairs": labelPairs,
                "groupBy": groupBy,
            },
        }

        if not groupdict.get('rpm1'):
            # The query is counting requests and not requests "per minute"
            model = {}

        transactionType = groupdict.get('transactionType')
        if transactionType:
            model = {}
            spanKind = 'span_kind=~"server|consumer", transaction_type="{}"'.format(transactionType)

        newQuery = "sum by (service{}) (increase(cube_apm_latency_count{{{}, {}}} default 0))".format(
            ',' + ','.join(groupBy) if groupBy else '', fragment, spanKind
        )

        return 'REQUEST_COUNT', newQuery, json.dumps(model), 1

    # error_rate ############################
    res = re.search(
        r"^\s*SELECT\s+(?P<rpm1>rate\s*\()?\s*count\s*\(\s*apm\.(?P<mType>service|key)\.transaction\.error\s*\)\s*(?P<rpm2>,\s*1\s+minute\s*\))?\s*(?:AS\s*(?:\w+|'[^']*'|\"[^\"]*\"))?\s*FROM\s+Metric\s+WHERE\s+" + idRegEx + r"\s*" + transactionTypeOptionalRegEx + r"\s*" + facetOptionalRegEx + r"\s*$",
        query, flags=re.IGNORECASE
    )
    if res:
        groupdict = res.groupdict()

        try:
            entityType, names = resolveEntityGuids(groupdict.get('idType'), groupdict.get('guid'), groupdict.get('guids'), all_entities)
        except Exception as ex:
            return "ERROR", "%s # %s" % (query, ex), '{}', 1

        if entityType == "APPLICATION":
            if groupdict.get('mType') != 'service':
                raise ValueError("unhandled situation: " + query)
        elif entityType == "KEY_TRANSACTION":
            if groupdict.get('mType') != 'key':
                raise ValueError("unhandled situation: " + query)
        else:
            raise ValueError("unhandled entity type " + entityType)
    
        fragment, labelPairs = makeEsr(entityType, names)

        spanKind = 'span_kind=~"server|consumer"'

        groupBy = parseFacet(entityType, groupdict.get('facet'))
        groupByStr = ' by ({})'.format(','.join(groupBy)) if groupBy else ''
        
        model = {
            "model": {
                "type": "quick",
                "calculate": "rpm",
                "value": "0",
                "labelPairs": labelPairs,
                "groupBy": groupBy,
            },
        }

        if not groupdict.get('rpm1'):
            # The query is counting errors and not errors "per minute"
            model = {}

        transactionType = groupdict.get('transactionType')
        if transactionType:
            model = {}
            spanKind = 'span_kind=~"server|consumer", transaction_type="{}"'.format(transactionType)

        newQuery = "sum by (service{}) (increase(cube_apm_latency_count{{{}, {}, status_code=\"ERROR\"}} default 0))".format(
            ',' + ','.join(groupBy) if groupBy else '', fragment, spanKind
        )

        return 'ERROR_RATE', newQuery, json.dumps(model), 1

    # latency_average ############################
    res = re.search(
        r"^\s*SELECT\s+(?P<rpm1>rate\s*\()?\s*average\s*\(\s*apm\.(?P<mType>service|key)\.transaction\.duration\s*\)\s*(?P<rpm2>,\s*1\s+minute\s*\))?\s*(?:AS\s*(?:\w+|'[^']*'|\"[^\"]*\"))?\s*FROM\s+Metric\s+WHERE\s+" + idRegEx + r"\s*" + transactionTypeOptionalRegEx + r"\s*" + facetOptionalRegEx + r"\s*$",
        query, flags=re.IGNORECASE
    )
    if res:
        groupdict = res.groupdict()

        try:
            entityType, names = resolveEntityGuids(groupdict.get('idType'), groupdict.get('guid'), groupdict.get('guids'), all_entities)
        except Exception as ex:
            return "ERROR", "%s # %s" % (query, ex), '{}', 1

        if entityType == "APPLICATION":
            if groupdict.get('mType') != 'service':
                raise ValueError("unhandled situation: " + query)
        elif entityType == "KEY_TRANSACTION":
            if groupdict.get('mType') != 'key':
                raise ValueError("unhandled situation: " + query)
        else:
            raise ValueError("unhandled entity type " + entityType)
    
        fragment, labelPairs = makeEsr(entityType, names)

        spanKind = 'span_kind=~"server|consumer"'

        groupBy = parseFacet(entityType, groupdict.get('facet'))
        groupByStr = ' by ({})'.format(','.join(groupBy)) if groupBy else ''
        
        model = {
            "model": {
                "type": "quick",
                "calculate": "avg",
                "value": "0",
                "labelPairs": labelPairs,
                "groupBy": groupBy,
            },
        }

        transactionType = groupdict.get('transactionType')
        if transactionType:
            model = {}
            spanKind = 'span_kind=~"server|consumer", transaction_type="{}"'.format(transactionType)

        newQuery = "sum by (service{}) (increase(cube_apm_latency_sum{{{}, {}}} default 0)) / sum by (service{}) (increase(cube_apm_latency_count{{{}, {}}} default 0))".format(
            ',' + ','.join(groupBy) if groupBy else '', fragment, spanKind,
            ',' + ','.join(groupBy) if groupBy else '', fragment, spanKind
        )

        return 'LATENCY_AVERAGE', newQuery, json.dumps(model), 1

    # latency_percentile ############################
    res = re.search(
        r"^\s*SELECT\s+(?P<rpm1>rate\s*\()?\s*percentile\s*\(\s*apm\.(?P<mType>service|key)\.transaction\.duration\s*,\s*(?P<percentile>\d+)\s*\)\s*(?P<rpm2>,\s*1\s+minute\s*\))?\s*(?:AS\s*(?:\w+|'[^']*'|\"[^\"]*\"))?\s*FROM\s+Metric\s+WHERE\s+" + idRegEx + r"\s*" + transactionTypeOptionalRegEx + r"\s*" + facetOptionalRegEx + r"\s*$",
        query, flags=re.IGNORECASE
    )
    if res:
        groupdict = res.groupdict()

        try:
            entityType, names = resolveEntityGuids(groupdict.get('idType'), groupdict.get('guid'), groupdict.get('guids'), all_entities)
        except Exception as ex:
            return "ERROR", "%s # %s" % (query, ex), '{}', 1

        if entityType == "APPLICATION":
            if groupdict.get('mType') != 'service':
                raise ValueError("unhandled situation: " + query)
        elif entityType == "KEY_TRANSACTION":
            if groupdict.get('mType') != 'key':
                raise ValueError("unhandled situation: " + query)
        else:
            raise ValueError("unhandled entity type " + entityType)
    
        fragment, labelPairs = makeEsr(entityType, names)

        spanKind = 'span_kind=~"server|consumer"'

        groupBy = parseFacet(entityType, groupdict.get('facet'))
        groupByStr = ' by ({})'.format(','.join(groupBy)) if groupBy else ''
        
        percentile = groupdict.get('percentile')
        if not percentile:
            raise ValueError("percentile not found in query: " + query)

        model = {
            "model": {
                "type": "quick",
                "calculate": "percentile",
                "value": percentile,
                "labelPairs": labelPairs,
                "groupBy": groupBy,
            },
        }

        transactionType = groupdict.get('transactionType')
        if transactionType:
            model = {}
            spanKind = 'span_kind=~"server|consumer", transaction_type="{}"'.format(transactionType)

        newQuery = "histogram_quantile({}, sum by (service,vmrange{}) (increase(cube_apm_latency_bucket{{{}, {}}} default 0)))".format(
            float(percentile) / 100.0, ',' + ','.join(groupBy) if groupBy else '', fragment, spanKind
        )

        return 'LATENCY_PERCENTILE', newQuery, json.dumps(model), 1

    # Unhandled query
    return "UNHANDLED", query, '{}', 1


def migrate(src_acct_id, mode):
    # Try to load extended entities first, fall back to regular entities
    try:
        entities = store.load_json_from_file('output', '%s_entities_extended.json' % str(src_acct_id))
        logger.info('Loaded extended entities file')
    except:
        try:
            entities = store.load_json_from_file('output', '%s_entities.json' % str(src_acct_id))
            logger.info('Loaded regular entities file')
        except:
            logger.warning('No entities file found, using empty list')
            entities = []
    
    dashboard_widgets = store.load_json_file(src_acct_id, "dashboards", 'dashboard_widgets.json')

    logger.info('Loaded %d entities and dashboard widgets' % (len(entities) if entities else 0))
    logger.info('Dashboard widgets structure: %s' % list(dashboard_widgets.keys()))

    cubeapm_dashboards = []
    
    for dashboard_info in dashboard_widgets.get('dashboards', []):
        dashboard_name = dashboard_info.get('name', 'Unknown Dashboard')
        dashboard_definition = dashboard_info.get('definition', {})
        dashboard_widgets_data = dashboard_info.get('widgets', {})
        
        logger.info('Processing dashboard: %s' % dashboard_name)
        logger.info('Dashboard widgets data keys: %s' % list(dashboard_widgets_data.keys()))
        
        cubeapm_dashboard = {
            'name': dashboard_name,
            'description': dashboard_definition.get('description', ''),
            'pages': []
        }
        
        if 'pages' in dashboard_widgets_data:
            for page in dashboard_widgets_data['pages']:
                cubeapm_page = {
                    'name': page.get('name', 'Page 1'),
                    'description': page.get('description', ''),
                    'widgets': []
                }
                
                if 'widgets' in page:
                    logger.info('Found %d widgets in page %s' % (len(page['widgets']), page.get('name', 'Unknown')))
                    for widget in page['widgets']:
                        logger.info('Processing widget: %s' % widget.get('title', 'Unknown'))
                        cubeapm_widget = translate_widget(widget, entities, mode)
                        if cubeapm_widget:
                            cubeapm_page['widgets'].append(cubeapm_widget)
                            logger.info('Added widget with %d queries' % len(cubeapm_widget.get('queries', [])))
                
                cubeapm_dashboard['pages'].append(cubeapm_page)
        
        cubeapm_dashboards.append(cubeapm_dashboard)
    
    save_cubeapm_dashboards(str(src_acct_id), cubeapm_dashboards, mode)


def translate_widget(widget, entities, mode):
    # Determine widget type from visualization ID
    visualization = widget.get('visualization', {})
    visualization_id = visualization.get('id', '') if visualization else ''
    raw_config = widget.get('rawConfiguration', {})
    
    cubeapm_widget = {
        'id': widget.get('id', ''),
        'title': widget.get('title', ''),
        'type': translate_widget_type(visualization_id),
        'position': widget.get('position', {}),
        'layout': widget.get('layout', {})
    }
    
    # Process NRQL queries if they exist
    if 'nrqlQueries' in raw_config:
        cubeapm_widget['queries'] = []
        for query in raw_config['nrqlQueries']:
            if 'query' in query:
                query_type, translated_query, model, priority = mapQuery(query['query'], entities)
                cubeapm_widget['queries'].append({
                    'type': query_type,
                    'query': translated_query,
                    'model': json.loads(model) if model != '{}' else {},
                    'priority': priority
                })
                logger.info('Translated query: %s -> %s' % (query['query'], translated_query))
    
    # Handle markdown widgets
    elif 'text' in raw_config:
        cubeapm_widget['content'] = raw_config.get('text', '')
    
    # Handle metric widgets
    elif 'value' in raw_config:
        cubeapm_widget['value'] = raw_config.get('value', 0)
        cubeapm_widget['unit'] = raw_config.get('unit', '')
    
    return cubeapm_widget


def translate_widget_type(visualization_id):
    # New Relic visualization ID mapping
    widget_type_map = {
        'viz.line': 'linechart',
        'viz.bar': 'linechart_area',
        'viz.pie': 'linechart_area',
        'viz.table': 'table',
        'viz.area': 'linechart_area',
        'viz.billboard': 'scorecard',
        'viz.markdown': 'ignore',
        'viz.stacked-bar': 'linechart_area',
        # 'viz.heatmap': 'chart',
        # 'viz.funnel': 'chart',
        # 'viz.histogram': 'chart'
    }
    return widget_type_map.get(visualization_id, 'chart')


def save_cubeapm_dashboards(account_id, cubeapm_dashboards, mode):
    output_file = '%s_cubeapm_dashboards.json' % account_id
    store.save_json(Path("output"), output_file, cubeapm_dashboards)
    logger.info('Saved %d CubeAPM dashboards to %s' % (len(cubeapm_dashboards), output_file))


def create_argument_parser():
    parser = argparse.ArgumentParser(
        description='Convert New Relic dashboards to CubeAPM format'
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
        '--mode',
        nargs=1,
        type=str,
        required=True,
        help='Database mode: mysql or postgresql',
        dest='mode'
    )
    return parser


def main():
    parser = create_argument_parser()
    args = parser.parse_args()
    mode = args.mode[0] if args.mode else 'mysql'
    
    if mode not in ['mysql', 'postgresql']:
        logger.error('Mode must be either mysql or postgresql')
        return

    migrate(args.source_account_id[0], mode)


def dquote(str):
    return str.replace('"', '\\"')


def squoteSQL(str, mode):
    if mode == 'mysql':
        return str.replace("'", "\\'")
    else:
        return str.replace("'", "''")


def requote(str_list):
    return '|'.join([re.escape(x) for x in str_list])


if __name__ == '__main__':
    main() 