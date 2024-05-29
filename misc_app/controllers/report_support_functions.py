# functions that support the generation of reports
import calendar
from datetime import date, timedelta, datetime


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


def make_month_range(start: date, end: date):
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


def make_quarter_range(start: int, end: int):
    """
    - Generates the quarters to consider
    """
    # list quarters in the format (
    # start date,
    # start month,
    # end date,
    # end month,
    # quarter number
    # )
    quarters = [
        (1, 1, 31, 3, "Q1"),
        (1, 4, 30, 6, "Q2"),
        (1, 7, 30, 9, "Q3"),
        (1, 10, 31, 12, "Q4")
    ]
    quarter_dates = []

    present_year = start
    today_date = datetime.now().date()
    while present_year <= end:
        for quarter in quarters:
            q_start_date = date(
                present_year,
                quarter[1],
                quarter[0]
            )
            if q_start_date < today_date:
                quarter_dates.append(
                    {
                        "start": q_start_date,
                        "end": date(
                            present_year,
                            quarter[3],
                            quarter[2]
                        ),
                        "quarter": f"{quarter[4]}-{str(present_year)}"
                    }
                )
        present_year += 1

    return quarter_dates
