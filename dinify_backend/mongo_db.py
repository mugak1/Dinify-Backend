"""
the connection to mongo db
"""
from pymongo import MongoClient
from decouple import config

MONGO_CLIENT = MongoClient(config("MONGO_HOST"), uuidRepresentation='standard')
MONGO_DB = MONGO_CLIENT[config("MONGO_DATABASE")]

# the collection strings for mongodb
ACTION_LOGS = 'action_logs'
NOTIFICATIONS = 'notifications'
COL_DPO_TOKENS = 'dpo_tokens'
COL_DPO_TOKEN_VERIFICATION = 'dpo_token_verification'

COL_PROFILE_UPDATE_APPROVALS = 'profile_update_approvals'
COL_NOTIFICATIONS = 'notifications'

# aggregator responses
COL_DPO_RESPONSES = 'dpo_responses'
COL_YO_RESPONSES = 'yo_responses'
