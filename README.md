# DINIFY BACKEND
The backend application for dinify_project.

# Getting started
- Knowledge of Python, Django, and REST APIs is required.
- If you know django, then you know what to do.

# code organisation | overview of directories
The directory organisation builds on top of django's default directory structure. The overall structure is as follows:
```
xxx_app
|__controllers i.e. where the actual functions are
|__endpoints i.e. where the endpoints are defined
|__management i.e. where the management commands are defined
```

# Understanding file and function naming conventions
- ConXXX - Controller class.
- SerializerXXX - Serializer class.


# Background tasks
- Determine customer who has made the order | `determine-customers`
- Verify DPO tokens | `verify-dpo-tokens`
- Processing Yo responses | `process_yo_responses`
- Check transaction status with aggregator | `check_transaction_statuses`
- Processing transactions | `process_transactions`

# TODO
- Refactor serializer files to smaller manageable ones, probably model based files.
- Refactor to minify endpoint handler files. this may go hand in hand with the refactor to adopt Class-scoped functions.
- Refactor to adopt class implementations for the various functions. Apart from misc/controller functions, many of these functions are likely in the same files or in the same controller folders.
- Refactor to have a single file for string definitions. This may be a huge file but best to keep all string in one location.
- Refactor to have single file for definitions of non-variable dependent messages.