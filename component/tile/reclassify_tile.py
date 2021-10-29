from sepal_ui import reclassify as rec
from sepal_ui import sepalwidgets as sw

from component import parameter as cp


class ReclassifyTile(rec.ReclassifyView):
    def __init__(self):

        super().__init__(
            gee=True,
            default_class={"UNCCD": str(cp.utils_dir / "UNCCD.csv")},
            save=True,
        )

        # change the title
        self.title.children[0].children = ["Adapt Land Cover map"]

        # remove the custom option
        tmp_list = self.w_default.children.copy()
        self.w_default.children = tmp_list[1:]

        # select UNCCD by default
        self.w_default.children[0].fire_event("click", None)

        # remove optional panel
        self.w_optional.class_ = "d-none"

        # change the metadata of the tile
        self._metadata = {"mount_id": "reclassify_tile"}