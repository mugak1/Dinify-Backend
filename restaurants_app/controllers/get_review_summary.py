from decimal import Decimal, ROUND_HALF_UP

from orders_app.models import Order


def get_review_summary(restaurant_id) -> dict:
    ratings = Order.objects.values('rating').filter(
        restaurant=restaurant_id
    ).exclude(rating__isnull=True)

    total_ratings = len(ratings)

    if total_ratings == 0:
        return {
            'total_ratings': 0,
            'one_star_percent': 0,
            'two_star_percent': 0,
            'three_star_percent': 0,
            'four_star_percent': 0,
            'five_star_percent': 0,
            'average_rating': 0
        }

    total = Decimal(str(total_ratings))
    one_star = len([r for r in ratings if r['rating'] == 1])
    two_star = len([r for r in ratings if r['rating'] == 2])
    three_star = len([r for r in ratings if r['rating'] == 3])
    four_star = len([r for r in ratings if r['rating'] == 4])
    five_star = len([r for r in ratings if r['rating'] == 5])

    one_star_percent = (Decimal(str(one_star)) / total * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
    two_star_percent = (Decimal(str(two_star)) / total * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
    three_star_percent = (Decimal(str(three_star)) / total * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
    four_star_percent = (Decimal(str(four_star)) / total * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
    five_star_percent = (Decimal(str(five_star)) / total * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)

    rating_sum = sum(r['rating'] for r in ratings)
    average_rating = (Decimal(str(rating_sum)) / total).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)

    return {
        'total_ratings': total_ratings,
        'one_star_percent': float(one_star_percent),
        'two_star_percent': float(two_star_percent),
        'three_star_percent': float(three_star_percent),
        'four_star_percent': float(four_star_percent),
        'five_star_percent': float(five_star_percent),
        'average_rating': float(average_rating)
    }
