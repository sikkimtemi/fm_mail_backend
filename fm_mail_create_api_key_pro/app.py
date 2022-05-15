import boto3
from boto3.dynamodb.conditions import Key
import os
import stripe
import uuid
from chalice import Chalice, CognitoUserPoolAuthorizer, Response

app = Chalice(app_name='fm_mail_create_api_key_pro')
app.debug = True

# 環境変数
USER_POOL_ARN = os.environ.get('USER_POOL_ARN')
USER_POOL_NAME = os.environ.get('USER_POOL_NAME')
DYNAMODB_API_KEY_TABLE = os.environ.get('DYNAMODB_API_KEY_TABLE')
DYNAMODB_STRIPE_TABLE = os.environ.get('DYNAMODB_STRIPE_TABLE')
REGION_NAME = os.environ.get('REGION_NAME')
STRIPE_API_KEY = os.environ.get('STRIPE_API_KEY')
CHALICE_DOMAIN = os.environ.get('CHALICE_DOMAIN')
MY_DOMAIN = os.environ.get('MY_DOMAIN')
REST_API_ID = os.environ['REST_API_ID']
USAGE_PLAN_ID = os.environ['USAGE_PLAN_ID']

# Stripe初期設定
stripe.api_key = STRIPE_API_KEY

# Cognitoで認証する
authorizer = CognitoUserPoolAuthorizer(
    USER_POOL_NAME,
    provider_arns=[USER_POOL_ARN]
)

# DynamoDBに接続
dynamodb = boto3.resource('dynamodb', region_name=REGION_NAME)
api_key_table = dynamodb.Table(DYNAMODB_API_KEY_TABLE)
stripe_table = dynamodb.Table(DYNAMODB_STRIPE_TABLE)

# API Gatewayの設定用クライアント
apigateway_cli = boto3.client('apigateway')


@app.route('/create-checkout-session/{lookup_key}/{user_name}', cors=True)
def create_checkout_session(lookup_key, user_name):
    # 検索キーから価格を取得する
    prices = stripe.Price.list(
        lookup_keys=[lookup_key],
        expand=['data.product']
    )

    # ワンタイムキーを生成する
    one_time_key = str(uuid.uuid4())

    # Stripeのセッションを作成する（success_urlにワンタイムキーが含まれているのがポイント）
    checkout_session = stripe.checkout.Session.create(
        line_items=[
            {
                'price': prices.data[0].id,
                'quantity': 1,
            },
        ],
        mode='subscription',
        success_url=CHALICE_DOMAIN +
        '/create-api-key/{CHECKOUT_SESSION_ID}/' + one_time_key,
        cancel_url=MY_DOMAIN + '/canceled_upgrade',
    )

    # セッションIDとUserNameをDynamoDBに登録する
    with stripe_table.batch_writer() as batch:
        batch.put_item(Item={"SessionID": checkout_session.id,
                            "UserName": user_name, "PaidFlag": False, "OneTimeKey": one_time_key})

    # Stripeに遷移する
    return Response(
        status_code=302,
        body='',
        headers={'Location': checkout_session.url})


@app.route('/create-api-key/{session_id}/{one_time_key}', cors=True)
def create_api_key(session_id, one_time_key):
    # チェックアウトセッションの状態を確認
    checkout_session = stripe.checkout.Session.retrieve(session_id)
    payment_status = checkout_session.payment_status

    # 支払い済みでなければキャンセルページに遷移
    cancel_url = MY_DOMAIN + '/canceled_upgrade'
    if payment_status != 'paid':
        return Response(
            status_code=302,
            body='',
            headers={'Location': cancel_url})

    # DynamoDBからセッションIDに紐づく情報を取り出す
    result = stripe_table.get_item(Key={'SessionID': session_id})
    user_name = result['Item']['UserName']
    paid_flag = result['Item']['PaidFlag']
    one_time_key_dynamo_db = result['Item']['OneTimeKey']

    # ワンタイムキーが一致しない場合、もしくはAPIキー発行済みの場合はキャンセルページに遷移
    if one_time_key != one_time_key_dynamo_db or paid_flag:
        return Response(
            status_code=302,
            body='',
            headers={'Location': cancel_url})

    # APIキーを発行
    result = apigateway_cli.create_api_key(
        name='fm_mail_pro_' + user_name,
        enabled=True,
        stageKeys=[
            {
                'restApiId': REST_API_ID,
                'stageName': 'api'
            }
        ]
    )

    # 発行したAPIキーの値とIDを取得
    api_key = result['value']
    api_key_id = result['id']

    # APIキーに使用量プランを適用
    apigateway_cli.create_usage_plan_key(
        usagePlanId=USAGE_PLAN_ID,
        keyId=api_key_id,
        keyType='API_KEY'
    )

    # DynamoDBにAPIキーを登録
    with api_key_table.batch_writer() as batch:
        batch.put_item(Item={"UserID": user_name,
                            "Type": "PRO", "ApiKey": api_key})

    # DynamoDBの支払い済みフラグを更新
    stripe_table.update_item(Key={'SessionID': session_id}, ExpressionAttributeNames={
                            "#PaidFlag": "PaidFlag"}, ExpressionAttributeValues={":PaidFlag": True}, UpdateExpression="SET #PaidFlag = :PaidFlag")

    # ToDo: ユーザープールのカスタム属性を更新する（user_typeとstripe_session_id）

    # サンクスページに遷移する
    success_url = MY_DOMAIN + '/thanks_upgrade'

    return Response(
        status_code=302,
        body='',
        headers={'Location': success_url})


@app.route('/create-billing-portal/{session_id}', authorizer=authorizer, cors=True)
def create_billing_portal(session_id):
    # チェックアウトセッションの状態を確認
    checkout_session = stripe.checkout.Session.retrieve(session_id)
    payment_status = checkout_session.payment_status

    # 支払い済みでなければ汎用キャンセルページに遷移
    if payment_status != 'paid':
        cancel_url = MY_DOMAIN + '/cancel'
        return Response(
            status_code=302,
            body='',
            headers={'Location': cancel_url})

    # 認証情報からUserNameを取り出す
    context = app.current_request.context
    user_name = context['authorizer']['claims']['cognito:username']

    # DynamoDBからセッションIDに紐づくUserNameを取り出す
    result = api_key_table.get_item(Key={'SessionId': session_id})
    user_name_dynamo_db = result['Item']['UserName']

    # 認証情報のUserNameとDynamoDBのUserNameが一致しなければキャンセルページに遷移
    if user_name != user_name_dynamo_db:
        return Response(
            status_code=302,
            body='',
            headers={'Location': cancel_url})

    # 請求ポータルに遷移
    return_url = MY_DOMAIN + '/thanks_upgrade'
    portal_session = stripe.billing_portal.Session.create(
        customer=checkout_session.customer,
        return_url=return_url,
    )
    billing_portal_url = portal_session.url
    return Response(
        status_code=302,
        body='',
        headers={'Location': billing_portal_url})
