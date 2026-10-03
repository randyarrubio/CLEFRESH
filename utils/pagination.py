from django.core.paginator import Paginator


def paginate(request, queryset, per_page=25):
    """Return (page_obj, page_qs): the requested page, plus the other GET params
    (filters) to carry into the page links rendered by includes/pagination.html."""
    page_obj = Paginator(queryset, per_page).get_page(request.GET.get('page'))
    params = request.GET.copy()
    params.pop('page', None)
    return page_obj, params.urlencode()
