# functions that support the generation of reports


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
