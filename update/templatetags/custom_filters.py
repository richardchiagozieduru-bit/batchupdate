from django import template

register = template.Library()


@register.filter
def get(dictionary, key):
    """Get value from dictionary by key"""
    if dictionary is None:
        return None
    return dictionary.get(key, '')
