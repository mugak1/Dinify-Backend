# DINIFY BACKEND
The backend application for dinify_project.

## Getting started

## code organisation


## Background tasks
- Determine customer who has made the order...`python manage.py determine-customers`
- Verify DPO tokens...`python manage.py verify-dpo-tokens`

/home/venv/bin/python /home/dinify_backend/manage.py determine-customers
/home/venv/bin/python /home/dinify_backend/manage.py verify-dpo-tokens

/home/scripts/determinecustomers.sh > /home/script_logs/determine_customers/`date +\%Y-\%m-\%d`.log 2>&1

/home/scripts/verifydpotokens.sh > /home/script_logs/verify_dpo_tokens/`date +\%Y-\%m-\%d`.log 2>&1
