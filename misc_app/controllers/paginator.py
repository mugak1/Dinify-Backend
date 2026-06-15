"""
implementation for project paginator
"""
from dataclasses import dataclass
from django.core.paginator import Paginator, EmptyPage, InvalidPage


@dataclass
class DinifyPaginator:
    """
    Class for paginating records
    - `args` dict should be: `{request, records}`
    """
    args: dict

    def paginate(self):
        """
        paginate records based on the request information
        """
        request = self.args.get('request')
        records = self.args.get('records')

        # Floor both params to 1: a 0/negative page_size would otherwise hit
        # Paginator(per_page=0) -> ZeroDivisionError in num_pages (uncaught ->
        # 500), and a 0/negative page is a nonsensical request that should
        # land on page 1 rather than an empty out-of-range page.
        try:
            page_size = max(1, int(request.GET.get('page_size', 25)))
        except (ValueError, TypeError):
            page_size = 25
        try:
            page_number = max(1, int(request.GET.get('page', 1)))
        except (ValueError, TypeError):
            page_number = 1

        paginated = Paginator(records, page_size)
        try:
            present_page = paginated.page(page_number)
            return {
                'records': present_page.object_list,
                'pagination': {
                    'paginated': True,
                    'total_records': len(records),
                    'number_of_pages': paginated.num_pages,
                    'page_size': page_size,
                    'current_page': page_number,
                    'has_next': present_page.has_next(),
                    'has_previous': present_page.has_previous()
                }
            }
        except (EmptyPage, InvalidPage):
            return {
                'records': [],
                'pagination': {
                    'paginated': True,
                    'total_records': len(records),
                    'number_of_pages': paginated.num_pages,
                    'page_size': page_size,
                    'current_page': page_number,
                    'has_next': False,
                    'has_previous': False
                }
            }
