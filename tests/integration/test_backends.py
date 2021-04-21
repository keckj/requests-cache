# TODO: Refactor with pytest fixtures
import pytest
from typing import Type

from requests_cache.backends.base import BaseStorage


class BaseStorageTestCase:
    """Base class for testing backends"""

    def __init__(
        self,
        *args,
        storage_class: Type[BaseStorage],
        picklable: bool = False,
        **kwargs,
    ):
        self.storage_class = storage_class
        self.picklable = picklable
        super().__init__(*args, **kwargs)

    NAMESPACE = 'pytest-temp'
    TABLES = ['table%s' % i for i in range(5)]

    def tearDown(self):
        for table in self.TABLES:
            self.storage_class(self.NAMESPACE, table).clear()
        super().tearDown()

    def test_set_get(self):
        d1 = self.storage_class(self.NAMESPACE, self.TABLES[0])
        d2 = self.storage_class(self.NAMESPACE, self.TABLES[1])
        d3 = self.storage_class(self.NAMESPACE, self.TABLES[2])
        d1['key_1'] = 1
        d2['key_2'] = 2
        d3['key_3'] = 3
        assert list(d1.keys()) == ['key_1']
        assert list(d2.keys()) == ['key_2']
        assert list(d3.keys()) == ['key_3']

        with pytest.raises(KeyError):
            d1[4]

    def test_str(self):
        d = self.storage_class(self.NAMESPACE)
        d.clear()
        d['key_1'] = 'value_1'
        d['key_2'] = 'value_2'
        assert dict(d) == {'key_1': 'value_1', 'key_2': 'value_2'}

    def test_del(self):
        d = self.storage_class(self.NAMESPACE)
        d.clear()
        for i in range(5):
            d[f'key_{i}'] = i
        del d['key_0']
        del d['key_1']
        del d['key_2']
        assert set(d.keys()) == {f'key_{i}' for i in range(3, 5)}
        assert set(d.values()) == set(range(3, 5))

        with pytest.raises(KeyError):
            del d['key_0']

    def test_picklable_dict(self):
        if self.picklable:
            d = self.storage_class(self.NAMESPACE)
            d['key_1'] = Picklable()
            d = self.storage_class(self.NAMESPACE)
            assert d['key_1'].a == 1
            assert d['key_1'].b == 2

    def test_clear_and_work_again(self):
        d1 = self.storage_class(self.NAMESPACE)
        d2 = self.storage_class(self.NAMESPACE, connection=d1.connection)
        d1.clear()
        d2.clear()

        for i in range(5):
            d1[i] = i
            d2[i] = i

        assert len(d1) == len(d2) == 5
        d1.clear()
        d2.clear()
        assert len(d1) == len(d2) == 0

    def test_same_settings(self):
        d1 = self.storage_class(self.NAMESPACE)
        d2 = self.storage_class(self.NAMESPACE, connection=d1.connection)
        d1.clear()
        d2.clear()
        d1['key_1'] = 1
        d2['key_2'] = 2
        assert d1 == d2


class Picklable:
    a = 1
    b = 2
