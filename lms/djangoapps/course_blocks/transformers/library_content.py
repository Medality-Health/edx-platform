"""
Content Library Transformer.
"""


import json
import logging
import pkg_resources  # @medality_custom

from eventtracking import tracker

from xblock.fields import Scope  # @medality_custom
from common.djangoapps.track import contexts
from lms.djangoapps.courseware.models import StudentModule
from openedx.core.djangoapps.content.block_structure.transformer import (
    BlockStructureTransformer,
    FilteringTransformerMixin
)
from xmodule.library_content_module import LibraryContentBlock  # lint-amnesty, pylint: disable=wrong-import-order
from xmodule.modulestore.django import modulestore  # lint-amnesty, pylint: disable=wrong-import-order

from ..utils import get_student_module_as_dict

logger = logging.getLogger(__name__)


# @medality_custom: start
def is_library_block(block_key):
    return block_key.block_type in ["library_content", "select_from_library"]


def get_xblock_class(block_type):
    """
    Returns the class associated to the entry point of the given block type
    """
    group = "xblock.v1"
    for entry_point in pkg_resources.iter_entry_points(group):
        if entry_point.name == block_type:
            return pkg_resources.load_entry_point(entry_point.dist, group, block_type)
    return None

# @medality_custom: end


class ContentLibraryTransformer(FilteringTransformerMixin, BlockStructureTransformer):
    """
    A transformer that manipulates the block structure by removing all
    blocks within a library_content module to which a user should not
    have access.

    Staff users are not to be exempted from library content pathways.
    """
    WRITE_VERSION = 1
    READ_VERSION = 1

    @classmethod
    def name(cls):
        """
        Unique identifier for the transformer's class;
        same identifier used in setup.py.
        """
        return "library_content"

    @classmethod
    def collect(cls, block_structure):
        """
        Collects any information that's necessary to execute this
        transformer's transform method.
        """
        block_structure.request_xblock_fields('mode')
        block_structure.request_xblock_fields('max_count')
        block_structure.request_xblock_fields('category')
        store = modulestore()

        # needed for analytics purposes
        def summarize_block(usage_key):
            """ Basic information about the given block """
            orig_key, orig_version = store.get_block_original_usage(usage_key)
            return {
                "usage_key": str(usage_key),
                "original_usage_key": str(orig_key) if orig_key else None,
                "original_usage_version": str(orig_version) if orig_version else None,
            }

        # For each block check if block is library_content.
        # If library_content add children array to content_library_children field
        for block_key in block_structure.topological_traversal(
                filter_func=is_library_block,  # @medality_custom
                yield_descendants_of_unyielded=True,
        ):
            xblock = block_structure.get_xblock(block_key)
            for child_key in xblock.children:
                summary = summarize_block(child_key)
                block_structure.set_transformer_block_field(child_key, cls, 'block_analytics_summary', summary)

    def transform_block_filters(self, usage_info, block_structure):
        all_library_children = set()
        all_selected_children = set()
        for block_key in block_structure:
            # @medality_custom
            if not is_library_block(block_key):
                continue
            xblock_class = get_xblock_class(block_key.block_type)
            library_children = block_structure.get_children(block_key)
            if library_children:
                all_library_children.update(library_children)
                selected = []
                mode = block_structure.get_xblock_field(block_key, 'mode')
                max_count = block_structure.get_xblock_field(block_key, 'max_count')

                # @medality_custom: start
                # Retrieve the "selected" list based on the selected field's scope within this xblock
                # For example: LibraryContentBlock stores selected in the user_state
                # while SelectFromLibraryXBlock stores in the xblock settings
                selected_prop = getattr(xblock_class, "selected", None)
                if selected_prop and selected_prop.scope == Scope.settings:
                    selected = modulestore().get_item(block_key).selected
                else:
                    # Retrieve "selected" json from LMS MySQL database.
                    state_dict = get_student_module_as_dict(usage_info.user, usage_info.course_key, block_key)
                    for selected_block in state_dict.get('selected', []):
                        # Add all selected entries for this user for this
                        # library module to the selected list.
                        block_type, block_id = selected_block
                        usage_key = usage_info.course_key.make_usage_key(block_type, block_id)
                        if usage_key in library_children:
                            selected.append(selected_block)

                    # Update selected
                    previous_count = len(selected)
                    # Since multiple xblock types are now supported, first retrieve the correct class
                    # for the current block_type in order to call the appropriate classmethod
                    block_keys = xblock_class.make_selection(
                        selected, library_children, max_count, mode
                    )
                    selected = block_keys['selected']

                    # Save back any changes
                    if any(block_keys[changed] for changed in ('invalid', 'overlimit', 'added')):
                        state_dict['selected'] = selected
                        StudentModule.save_state(
                            student=usage_info.user,
                            course_id=usage_info.course_key,
                            module_state_key=block_key,
                            defaults={
                                'state': json.dumps(state_dict),
                            },
                        )

                    # publish events for analytics
                    self._publish_events(
                        block_structure,
                        block_key,
                        previous_count,
                        max_count,
                        block_keys,
                        usage_info.user.id,
                    )
                
                # @medality_custom: end
                all_selected_children.update(usage_info.course_key.make_usage_key(s[0], s[1]) for s in selected)

        def check_child_removal(block_key):
            """
            Return True if selected block should be removed.

            Block is removed if it is part of library_content, but has
            not been selected for current user.
            """
            if block_key not in all_library_children:
                return False
            if block_key in all_selected_children:
                return False
            return True

        return [block_structure.create_removal_filter(check_child_removal)]

    def _publish_events(self, block_structure, location, previous_count, max_count, block_keys, user_id):
        """
        Helper method to publish events for analytics purposes
        """

        def format_block_keys(keys):
            """
            Helper function to format block keys
            """
            json_result = []
            for key in keys:
                info = block_structure.get_transformer_block_field(
                    key, ContentLibraryTransformer, 'block_analytics_summary'
                )
                json_result.append(info)
            return json_result

        def publish_event(event_name, result, **kwargs):
            """
            Helper function to publish an event for analytics purposes
            """
            event_data = {
                "location": str(location),
                "previous_count": previous_count,
                "result": result,
                "max_count": max_count,
            }
            event_data.update(kwargs)
            context = contexts.course_context_from_course_id(location.course_key)
            if user_id:
                context['user_id'] = user_id
            full_event_name = f"edx.librarycontentblock.content.{event_name}"
            with tracker.get_tracker().context(full_event_name, context):
                tracker.emit(full_event_name, event_data)

        LibraryContentBlock.publish_selected_children_events(
            block_keys,
            format_block_keys,
            publish_event,
        )


class ContentLibraryOrderTransformer(BlockStructureTransformer):
    """
    A transformer that manipulates the block structure by modifying the order of the
    selected blocks within a library_content module to match the order of the selections
    made by the ContentLibraryTransformer or the corresponding XBlock. So this transformer
    requires the selections for the randomized content block to be already
    made either by the ContentLibraryTransformer or the XBlock.

    Staff users are *not* exempted from library content pathways.
    """
    WRITE_VERSION = 1
    READ_VERSION = 1

    @classmethod
    def name(cls):
        """
        Unique identifier for the transformer's class;
        same identifier used in setup.py
        """
        return "library_content_randomize"

    @classmethod
    def collect(cls, block_structure):
        """
        Collects any information that's necessary to execute this
        transformer's transform method.
        """
        # There is nothing to collect
        pass  # pylint:disable=unnecessary-pass

    def transform(self, usage_info, block_structure):
        """
        Transforms the order of the children of the randomized content block
        to match the order of the selections made and stored in the XBlock 'selected' field.
        """
        for block_key in block_structure:
            # @medality_custom
            if not is_library_block(block_key):
                continue

            library_children = block_structure.get_children(block_key)

            if library_children:
                current_children_blocks = {block.block_id for block in library_children}

                # @medality_custom: start
                # Retrieve the "selected" list based on the selected field's scope within this xblock
                # For example: LibraryContentBlock stores selected in the user_state
                # while SelectFromLibraryXBlock stores in the xblock settings
                selected = []
                xblock_class = get_xblock_class(block_key.block_type)
                selected_prop = getattr(xblock_class, "selected", None)
                if selected_prop and selected_prop.scope == Scope.settings:
                    selected = modulestore().get_item(block_key).selected
                else:
                    # Retrieve "selected" json from LMS MySQL database.
                    state_dict = get_student_module_as_dict(usage_info.user, usage_info.course_key, block_key)
                    selected = state_dict.get('selected', [])

                current_selected_blocks = {item[1] for item in selected}
                # @medality_custom: end

                # As the selections should have already been made by the ContentLibraryTransformer,
                # the current children of the library_content block should be the same as the stored
                # selections. If they aren't, some other transformer that ran before this transformer
                # has modified those blocks (for example, content gating may have affected this). So do not
                # transform the order in that case.
                if current_children_blocks != current_selected_blocks:
                    logger.debug(
                        'Mismatch between the children of %s in the stored state and the actual children for user %s. '
                        'Continuing without order transformation.',
                        str(block_key),
                        usage_info.user.username
                    )
                else:
                    # @medality_custom
                    ordering_data = {block[1]: position for position, block in enumerate(selected)}
                    library_children.sort(key=lambda block, data=ordering_data: data[block.block_id])
