import json
import os
import sys
from pathlib import Path

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
        raise SystemExit('Usage: python src/campaign_goal_inspection.py REQUEST_JSON RESULT_JSON')

    request = json.loads(Path(sys.argv[1]).read_text())
    output_path = Path(sys.argv[2])
    cid = clean_id(request['customer_id'])
    campaign_id = clean_id(request['campaign_id'])

    client = load_client()
    service = client.get_service('GoogleAdsService')

    campaign_rows = list(service.search(customer_id=cid, query=f'''
        SELECT
          campaign.id,
          campaign.name,
          campaign.status,
          campaign.advertising_channel_type,
          campaign.advertising_channel_sub_type,
          campaign.app_campaign_setting.bidding_strategy_goal_type,
          campaign.selective_optimization.conversion_actions
        FROM campaign
        WHERE campaign.id = {campaign_id}
    '''))

    if not campaign_rows:
        raise RuntimeError(f'Campaign {campaign_id} not found')

    campaign = campaign_rows[0].campaign

    campaign_goal_rows = list(service.search(customer_id=cid, query=f'''
        SELECT
          campaign_conversion_goal.campaign,
          campaign_conversion_goal.category,
          campaign_conversion_goal.origin,
          campaign_conversion_goal.biddable,
          campaign_conversion_goal.resource_name
        FROM campaign_conversion_goal
        WHERE campaign_conversion_goal.campaign = 'customers/{cid}/campaigns/{campaign_id}'
        ORDER BY campaign_conversion_goal.category, campaign_conversion_goal.origin
    '''))

    conversion_rows = list(service.search(customer_id=cid, query='''
        SELECT
          conversion_action.resource_name,
          conversion_action.id,
          conversion_action.name,
          conversion_action.status,
          conversion_action.type,
          conversion_action.category,
          conversion_action.origin,
          conversion_action.primary_for_goal,
          conversion_action.firebase_settings.event_name,
          conversion_action.google_analytics_4_settings.event_name
        FROM conversion_action
        ORDER BY conversion_action.id
    '''))

    actions = {}
    for row in conversion_rows:
        action = row.conversion_action
        actions[action.resource_name] = {
            'id': str(action.id),
            'resource_name': action.resource_name,
            'name': action.name,
            'status': action.status.name,
            'type': action.type_.name,
            'category': action.category.name,
            'origin': action.origin.name,
            'primary_for_goal': bool(action.primary_for_goal),
            'firebase_event_name': action.firebase_settings.event_name,
            'ga4_event_name': action.google_analytics_4_settings.event_name,
        }

    selective = list(campaign.selective_optimization.conversion_actions)
    selected_actions = [actions.get(r, {'resource_name': r, 'name': 'UNKNOWN'}) for r in selective]

    result = {
        'customer_id': cid,
        'campaign': {
            'id': str(campaign.id),
            'name': campaign.name,
            'status': campaign.status.name,
            'channel_type': campaign.advertising_channel_type.name,
            'channel_sub_type': campaign.advertising_channel_sub_type.name,
            'app_bidding_strategy_goal_type': campaign.app_campaign_setting.bidding_strategy_goal_type.name,
            'selective_optimization_conversion_actions': selective,
            'selected_conversion_action_details': selected_actions,
        },
        'campaign_conversion_goals': [
            {
                'resource_name': row.campaign_conversion_goal.resource_name,
                'category': row.campaign_conversion_goal.category.name,
                'origin': row.campaign_conversion_goal.origin.name,
                'biddable': bool(row.campaign_conversion_goal.biddable),
            }
            for row in campaign_goal_rows
        ],
        'relevant_conversion_actions': [
            action for action in actions.values()
            if action['category'] == 'DOWNLOAD'
            or action['firebase_event_name'] in {'first_open', 'ad_impression'}
            or action['ga4_event_name'] in {'first_open', 'ad_impression'}
            or action['name'].lower() in {'first_open', 'first open', 'ad_impression'}
            or 'install' in action['name'].lower()
        ],
    }

    output = {'ok': True, 'operation': request.get('operation'), 'result': result}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2))
    print(json.dumps(output, indent=2))


if __name__ == '__main__':
    main()
