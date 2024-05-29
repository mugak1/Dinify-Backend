# functions that support the generation of reports
import calendar
from datetime import date, timedelta


def make_graph_series_data(
    x_title: str,
    y_values: list,
    x_detail: str
) -> dict:
    graph_series = {}
    x_values = []
    for item in y_values:
        for key, value in item.items():
            if key not in graph_series and key != x_detail:
                graph_series[key] = []
            if key != x_detail:
                graph_series[key].append(value)
            else:
                x_values.append(value)
    series = [
        {
            "name": key.replace('_', ' ').title(),
            "data": values
        } for key, values in graph_series.items()
    ]

    return {
        'series': series,
        'xaxis': {
            'categories': x_values,
            'title': {'text': x_title}
        }
    }


def make_month_range(
    start: date,
    end: date
):
    """
    - Generates the month range to consider
    """
    # get month of the start and end dates
    start_year = start.year
    start_month = start.month
    end_year = end.year
    end_month = end.month

    # Get the dates of the month to consider
    first_month_range = calendar.monthrange(start_year, start_month)
    end_month_range = calendar.monthrange(end_year, end_month)

    months = []

    final_month_end_date = date(
        start_year,
        start_month,
        first_month_range[1]
    )

    final_end_date = date(
        end_year,
        end_month,
        end_month_range[1]
    )
    current_track_month = final_month_end_date
    while current_track_month <= final_end_date:
        current_track_month_range = calendar.monthrange(
            current_track_month.year,
            current_track_month.month
        )
        months.append(
            {
                'month_name': calendar.month_name[
                    current_track_month.month
                ],
                'month_number': current_track_month.month,
                'start_date': 1,
                'end_date': current_track_month_range[1],
                'year': current_track_month.year,
                'sd': date(
                    current_track_month.year,
                    current_track_month.month,
                    1
                ),
                'ed': date(
                    current_track_month.year,
                    current_track_month.month,
                    current_track_month_range[1]
                ),

            }
        )

        current_track_month = date(
            current_track_month.year,
            current_track_month.month,
            current_track_month_range[1]
        )
        current_track_month = current_track_month + timedelta(
            days=1
        )

    return months
