from typing import Optional
from misc_app.controllers.utils.check_non_unique_conflicts import check_non_unique_conflicts


class ConMiscUtils:
    """
    This class contains utility functions for the misc app
    """

    @staticmethod
    def check_non_unique_conflicts(
        model,
        unique_combination: list,
        fks: list,
        values: dict,
        error_message: str,
        existing_record_id: Optional[str] = None,
    ) -> dict:
        """
        Check for non-unique conflicts in the database
        """
        return check_non_unique_conflicts(
            model=model,
            unique_combination=unique_combination,
            fks=fks,
            values=values,
            error_message=error_message,
            existing_record_id=existing_record_id
        )
