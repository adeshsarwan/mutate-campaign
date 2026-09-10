import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.ads.googleads.client import GoogleAdsClient


def clean_id(value):
    return ''.join(ch for ch in str(value) if ch.isdigit())


def load_client():
    config = {
        'developer_token': os.environ['GOOGLE_ADS_DEVELOPER_TOKEN'],
        'client_id': os.environ['GOOGLE_ADS_CLIENT_ID'],
        'client_secret': os.environ['GOOGLE_ADS_CLIENT_SECRET'],
        'refresh_token': os.environ['GOOGLE_ADS_REFRESH_TOKEN'],
        'use_proto_plus': True,
    }
    login_customer_id = os.environ.get('GOOGLE_ADS_LOGIN_CUSTOMER_ID')
    if login_customer_id:
        config['login_customer_id'] = clean_id(login_customer_id)
    return GoogleAdsClient.load_from_dict(config)


def main():
    if len(sys.argv) != 3:
        raise SystemExit('Usage: python src/conversion_breakdown.py INPUT_JSON OUTPUT_JSON')

    request = json.load(open(sys.argv[1]))
    out_path = sys.argv[2]
    cid = clean_id(request['customer_id'])
    campaign_id = clean_id(request['campaign_id'])
    client = load_client()
    service = client.get_service('GoogleAdsService')

    customer_rows = list(service.search(customer_id=cid, query='''
        SELECT customer.currency_code, customer.time_zone
        FROM customer
        LIMIT 1
    '''))
    customer = customer_rows[0].customer
    tz_name = customer.time_zone or 'UTC'
    today = datetime.now(ZoneInfo(tz_name)).date()
    days = int(request.get('days', 7))
    start = (today - timedelta(days=days - 1)).isoformat()
    end = today.isoformat()

    breakdown_query = f'''
        SELECT
          campaign.id,
          campaign.name,
          segments.conversion_action,
          segments.conversion_action_name,
          metrics.conversions,
          metrics.conversions_value,
          metrics.all_conversions,
          metrics.all_conversions_value
        FROM campaign
        WHERE campaign.id = {campaign_id}
          AND segments.date BETWEEN '{start}' AND '{end}'
          AND metrics.all_conversions > 0
        ORDER BY metrics.all_conversions DESC
    '''
    breakdown_rows = list(service.search(customer_id=cid, query=breakdown_query))

    action_query = '''
        SELECT
          conversion_action.resource_name,
          conversion_action.id,
          conversion_action.name,
          conversion_action.status,
          conversion_action.type,
          conversion_action.category,
          conversion_action.origin,
          conversion_action.primary_for_goal,
          conversion_action.value_settings.default_value,
          conversion_action.value_settings.default_currency_code,
          conversion_action.value_settings.always_use_default_value,
          conversion_action.firebase_settings.event_name,
          conversion_action.firebase_settings.project_id,
          conversion_action.firebase_settings.property_id,
          conversion_action.google_analytics_4_settings.event_name,
          conversion_action.google_analytics_4_settings.property_id
        FROM conversion_action
    '''
    action_rows = list(service.search(customer_id=cid, query=action_query))
    actions = {}
    for row in action_rows:
        a = row.conversion_action
        actions[a.resource_name] = {
            'id': str(a.id),
            'name': a.name,
            'status': a.status.name,
            'type': a.type_.name,
            'category': a.category.name,
            'origin': a.origin.name,
            'primary_for_goal': bool(a.primary_for_goal),
            'default_value': float(a.value_settings.default_value),
            'default_currency_code': a.value_settings.default_currency_code,
            'always_use_default_value': bool(a.value_settings.always_use_default_value),
            'firebase_event_name': a.firebase_settings.event_name,
            'firebase_project_id': a.firebase_settings.project_id,
            'firebase_property_id': str(a.firebase_settings.property_id) if a.firebase_settings.property_id else '',
            'ga4_event_name': a.google_analytics_4_settings.event_name,
            'ga4_property_id': str(a.google_analytics_4_settings.property_id) if a.google_analytics_4_settings.property_id else '',
        }

    breakdown = []
    for row in breakdown_rows:
        resource = row.segments.conversion_action
        breakdown.append({
            'conversion_action_resource_name': resource,
            'conversion_action_name': row.segments.conversion_action_name,
            'conversions': float(row.metrics.conversions),
            'conversion_value': float(row.metrics.conversions_value),
            'all_conversions': float(row.metrics.all_conversions),
            'all_conversion_value': float(row.metrics.all_conversions_value),
            'action': actions.get(resource),
        })

    result = {
        'customer_id': cid,
        'campaign_id': campaign_id,
        'currency': customer.currency_code,
        'time_zone': tz_name,
        'start_date': start,
        'end_date': end,
        'breakdown': breakdown,
        'totals': {
            'conversions': sum(x['conversions'] for x in breakdown),
            'conversion_value': sum(x['conversion_value'] for x in breakdown),
            'all_conversions': sum(x['all_conversions'] for x in breakdown),
            'all_conversion_value': sum(x['all_conversion_value'] for x in breakdown),
        },
    }
    with open(out_path, 'w') as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
