"""
implementation for project paginator
"""
from dataclasses import dataclass
from django.core.paginator import Paginator


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

        page_size = request.GET.get('page_size', 25)
        page_number = request.GET.get('page', 1)

        paginated = Paginator(self.args.get('records'), page_size)
        present_page = paginated.page(page_number)

        return {
            'records': present_page.object_list,
            'pagination': {
                'paginated': True,
                'total_records': len(self.args.get('records')),
                'number_of_pages': paginated.num_pages,
                'page_size': page_size,
                'current_page': page_number,
                'has_next': present_page.has_next(),
                'has_previous': present_page.has_previous()
            }
        }
