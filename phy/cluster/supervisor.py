# -*- coding: utf-8 -*-

"""Manual clustering GUI component."""


# -----------------------------------------------------------------------------
# Imports
# -----------------------------------------------------------------------------

from functools import partial
import inspect
from itertools import chain
import logging

import numpy as np

from ._history import GlobalHistory
from ._utils import create_cluster_meta
from .clustering import Clustering

from phylib.utils import Bunch, emit, connect, unconnect
from phylib.utils.color import ClusterColorSelector
from phy.gui.actions import Actions
from phy.gui.qt import _block, set_busy, _wait
from phy.gui.widgets import Table, HTMLWidget, _uniq, Barrier

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Utility functions
# -----------------------------------------------------------------------------

def _process_ups(ups):  # pragma: no cover
    """This function processes the UpdateInfo instances of the two
    undo stacks (clustering and cluster metadata) and concatenates them
    into a single UpdateInfo instance."""
    if len(ups) == 0:
        return
    elif len(ups) == 1:
        return ups[0]
    elif len(ups) == 2:
        up = ups[0]
        up.update(ups[1])
        return up
    else:
        raise NotImplementedError()


def _ensure_all_ints(l):
    if (l is None or l == []):
        return
    for i in range(len(l)):
        l[i] = int(l[i])


# -----------------------------------------------------------------------------
# Tasks
# -----------------------------------------------------------------------------

class TaskLogger(object):
    """Internal object that gandles all clustering actions and the automatic actions that
    should follow as part of the "wizard".

    For example, merging two clusters in the cluster view and similarity view should
    automatically lead to the merged cluster being selected in the cluster view, and the
    next similar cluster selected in the similarity view.

    """
    def __init__(self, cluster_view=None, similarity_view=None, supervisor=None):
        self.cluster_view = cluster_view
        self.similarity_view = similarity_view
        self.supervisor = supervisor
        self._processing = False
        # List of tasks that have completed.
        self._history = []
        # Tasks that have yet to be performed.
        self._queue = []

    def enqueue(self, sender, name, *args, output=None, **kwargs):
        """Enqueue an action, which has a sender, a function name, a list of arguments,
        and an optional output."""
        logger.log(
            5, "Enqueue %s %s %s %s (%s)", sender.__class__.__name__, name, args, kwargs, output)
        self._queue.append((sender, name, args, kwargs))

    def dequeue(self):
        """Dequeue the oldest item in the queue."""
        return self._queue.pop(0) if self._queue else None

    def _callback(self, task, output):
        """Called after the execution of an action in the queue.

        Will add the action to the history, with its input, enqueue subsequent actions, and
        ensure these actions are immediately executed.

        """
        # Log the task and its output.
        self._log(task, output)
        # Find the post tasks after that task has completed, and enqueue them.
        self.enqueue_after(task, output)
        # Loop.
        self.process()

    def _eval(self, task):
        """Evaluate a task and call a callback function."""
        sender, name, args, kwargs = task
        logger.log(5, "Calling %s.%s(%s)", sender.__class__.__name__, name, args, kwargs)
        f = getattr(sender, name)
        callback = partial(self._callback, task)
        argspec = inspect.getfullargspec(f)
        argspec = argspec.args + argspec.kwonlyargs
        if 'callback' in argspec:
            f(*args, **kwargs, callback=callback)
        else:
            # HACK: use on_cluster event instead of callback.
            def _cluster_callback(tsender, up):
                self._callback(task, up)
            connect(_cluster_callback, event='cluster', sender=self.supervisor)
            f(*args, **kwargs)
            unconnect(_cluster_callback)

    def process(self):
        """Process all tasks in queue."""
        self._processing = True
        task = self.dequeue()
        if not task:
            self._processing = False
            return
        # Process the first task in queue, or stop if the queue is empty.
        self._eval(task)

    def enqueue_after(self, task, output):
        """Enqueue tasks after a given action."""
        sender, name, args, kwargs = task
        f = lambda *args, **kwargs: logger.log(5, "No method _after_%s", name)
        getattr(self, '_after_%s' % name, f)(task, output)

    def _after_merge(self, task, output):
        """Tasks that should follow a merge."""
        sender, name, args, kwargs = task
        merged, to = output.deleted, output.added[0]
        cluster_ids, next_cluster, similar, next_similar = self.last_state()
        # Update views after cluster_view.select event only if there is no similar clusters.
        # Otherwise, this is only the similarity_view that will raise the select event leading
        # to view updates.
        self.enqueue(self.cluster_view, 'select', [to], update_views=similar is None)
        if similar is None:
            return
        if set(merged).intersection(similar) and next_similar is not None:
            similar = [next_similar]
        self.enqueue(self.similarity_view, 'select', similar)

    def _after_split(self, task, output):
        """Tasks that should follow a split."""
        sender, name, args, kwargs = task
        self.enqueue(self.cluster_view, 'select', output.added)

    def _get_clusters(self, which):
        cluster_ids, next_cluster, similar, next_similar = self.last_state()
        if which == 'all':
            return _uniq(cluster_ids + similar)
        elif which == 'best':
            return cluster_ids
        elif which == 'similar':
            return similar
        return which

    def _after_move(self, task, output):
        """Tasks that should follow a move."""
        sender, name, args, kwargs = task
        which = output.metadata_changed
        moved = set(self._get_clusters(which))
        cluster_ids, next_cluster, similar, next_similar = self.last_state()
        cluster_ids = set(cluster_ids or ())
        similar = set(similar or ())
        # Move best.
        if moved <= cluster_ids:
            self.enqueue(self.cluster_view, 'next')
        # Move similar.
        elif moved <= similar:
            self.enqueue(self.similarity_view, 'next')
        # Move all.
        else:
            self.enqueue(self.cluster_view, 'next')
            self.enqueue(self.similarity_view, 'next')

    def _after_undo(self, task, output):
        """Task that should follow an undo."""
        last_action = self.last_task(name_not_in=('select', 'next', 'previous', 'undo', 'redo'))
        self._select_state(self.last_state(last_action))

    def _after_redo(self, task, output):
        """Task that should follow an redo."""
        last_undo = self.last_task('undo')
        # Select the last state before the last undo.
        self._select_state(self.last_state(last_undo))

    def _select_state(self, state):
        """Enqueue select actions when a state (selected clusters and similar clusters) is set."""
        cluster_ids, next_cluster, similar, next_similar = state
        self.enqueue(self.cluster_view, 'select', cluster_ids)
        if similar:
            self.enqueue(self.similarity_view, 'select', similar)

    def _log(self, task, output):
        """Add a completed task to the history stack."""
        sender, name, args, kwargs = task
        assert sender
        assert name
        logger.log(
            5, "Log %s %s %s %s (%s)", sender.__class__.__name__, name, args, kwargs, output)
        task = (sender, name, args, kwargs, output)
        # Avoid successive duplicates (even if sender is different).
        if not self._history or self._history[-1][1:] != task[1:]:
            self._history.append(task)

    def log(self, sender, name, *args, output=None, **kwargs):
        """Add a completed task to the history stack."""
        self._log((sender, name, args, kwargs), output)

    def last_task(self, name=None, name_not_in=()):
        """Return the last executed task."""
        for (sender, name_, args, kwargs, output) in reversed(self._history):
            if (name and name_ == name) or (name_not_in and name_ and name_ not in name_not_in):
                assert name_
                return (sender, name_, args, kwargs, output)

    def last_state(self, task=None):
        """Return (cluster_ids, next_cluster, similar, next_similar)."""
        cluster_state = (None, None)
        similarity_state = (None, None)
        h = self._history
        # Last state until the passed task, if applicable.
        if task:
            i = self._history.index(task)
            h = self._history[:i]
        for (sender, name, args, kwargs, output) in reversed(h):
            # Last selection is cluster view selection: return the state.
            if (sender == self.similarity_view and similarity_state == (None, None) and
                    name in ('select', 'next', 'previous')):
                similarity_state = (output['selected'], output['next']) if output else (None, None)
            if (sender == self.cluster_view and
                    cluster_state == (None, None) and
                    name in ('select', 'next', 'previous')):
                cluster_state = (output['selected'], output['next']) if output else (None, None)
                return (*cluster_state, *similarity_state)

    def show_history(self):
        """Show the history stack."""
        print("=== History ===")
        for sender, name, args, kwargs, output in self._history:
            print(
                '{: <24} {: <8}'.format(sender.__class__.__name__, name), *args, output, kwargs)

    def has_finished(self):
        """Return whether the queue has finished being processed."""
        return len(self._queue) == 0 and not self._processing


# -----------------------------------------------------------------------------
# Cluster view and similarity view
# -----------------------------------------------------------------------------

_CLUSTER_VIEW_STYLES = '''
table tr[data-group='good'] {
    color: #86D16D;
}

table tr[data-group='mua'] {
    color: #afafaf;
}

table tr[data-group='noise'] {
    color: #777;
}
'''


class ClusterView(Table):
    """Display a table of all clusters with metrics and labels as columns."""

    _required_columns = ('n_spikes',)
    _view_name = 'cluster_view'
    _styles = _CLUSTER_VIEW_STYLES

    def __init__(self, *args, data=None, columns=(), sort=None):
        # NOTE: debounce select events.
        HTMLWidget.__init__(
            self, *args, title=self.__class__.__name__, debounce_events=('select',))
        self._set_styles()
        self._reset_table(data=data, columns=columns, sort=sort)

    def _reset_table(self, data=None, columns=(), sort=None):
        """Recreate the table with specified columns, data, and sort."""
        emit(self._view_name + '_init', self)
        # Ensure 'id' is the first column.
        if 'id' in columns:
            columns.remove('id')
        columns = ['id'] + list(columns)
        # Add required columns if needed.
        for col in self._required_columns:
            if col not in columns:
                columns += [col]
            assert col in columns
        assert columns[0] == 'id'

        # Allow to have <tr data_group="good"> etc. which allows for CSS styling.
        value_names = columns + [{'data': ['group']}]
        # Default sort.
        sort = sort or ('n_spikes', 'desc')
        self._init_table(columns=columns, value_names=value_names, data=data, sort=sort)

    def _set_styles(self):
        self.builder.add_style(self._styles)

    def get_state(self, callback=None):
        """Return the cluster view state, with the current sort."""
        self.get_current_sort(lambda sort: callback({'current_sort': tuple(sort or (None, None))}))

    def set_state(self, state):
        """Set the cluster view state, with a specified sort."""
        sort_by, sort_dir = state.get('current_sort', (None, None))
        if sort_by:
            self.sort_by(sort_by, sort_dir)


class SimilarityView(ClusterView):
    """Display a table of clusters with metrics and labels as columns, and an additional
    similarity column.

    This view displays clusters similar to the clusters currently selected
    in the cluster view.

    Events
    ------

    * request_similar_clusters(cluster_id)

    """

    _required_columns = ('n_spikes', 'similarity')
    _view_name = 'similarity_view'

    def set_selected_index_offset(self, n):
        """Set the index of the selected cluster, used for correct coloring in the similarity
        view."""
        self.eval_js('table._setSelectedIndexOffset(%d);' % n)

    def reset(self, cluster_ids):
        """Recreate the similarity view, given the selected clusters in the cluster view."""
        if not len(cluster_ids):
            return
        similar = emit('request_similar_clusters', self, cluster_ids[-1])
        # Clear the table.
        if similar:
            self.remove_all_and_add(
                [cl for cl in similar[0] if cl['id'] not in cluster_ids])
        else:  # pragma: no cover
            self.remove_all()
        return similar


# -----------------------------------------------------------------------------
# ActionCreator
# -----------------------------------------------------------------------------

class ActionCreator(object):
    """Companion class to the Supervisor that manages the related GUI actions."""

    default_shortcuts = {
        # Clustering.
        'merge': 'g',
        'split': 'k',

        'label': 'l',

        # Move.
        'move_best_to_noise': 'alt+n',
        'move_best_to_mua': 'alt+m',
        'move_best_to_good': 'alt+g',
        'move_best_to_unsorted': 'alt+u',

        'move_similar_to_noise': 'ctrl+n',
        'move_similar_to_mua': 'ctrl+m',
        'move_similar_to_good': 'ctrl+g',
        'move_similar_to_unsorted': 'ctrl+u',

        'move_all_to_noise': 'ctrl+alt+n',
        'move_all_to_mua': 'ctrl+alt+m',
        'move_all_to_good': 'ctrl+alt+g',
        'move_all_to_unsorted': 'ctrl+alt+u',

        # Wizard.
        'reset': 'ctrl+alt+space',
        'next': 'space',
        'previous': 'shift+space',
        'next_best': 'down',
        'previous_best': 'up',

        # Misc.
        'undo': 'ctrl+z',
        'redo': ('ctrl+shift+z', 'ctrl+y'),
    }

    def __init__(self, supervisor=None):
        self.supervisor = supervisor

    def add(self, which, name, **kwargs):
        """Add an action to a given menu."""
        # This special keyword argument lets us use a different name for the
        # action and the event name/method (used for different move flavors).
        method_name = kwargs.pop('method_name', name)
        method_args = kwargs.pop('method_args', ())
        emit_fun = partial(emit, 'action', self, method_name, *method_args)
        f = getattr(self.supervisor, method_name, None)
        docstring = inspect.getdoc(f) if f else name
        if not kwargs.get('docstring', None):
            kwargs['docstring'] = docstring
        getattr(self, '%s_actions' % which).add(emit_fun, name=name, **kwargs)

    def attach(self, gui):
        """Attach the GUI and create the menus."""
        # Create the menus.
        self.edit_actions = Actions(
            gui, menu='&Edit', default_shortcuts=self.default_shortcuts)
        self.select_actions = Actions(
            gui, menu='Sele&ct', default_shortcuts=self.default_shortcuts)
        self.view_actions = Actions(
            gui, menu='&View', default_shortcuts=self.default_shortcuts)

        # Create the actions.
        self._create_edit_actions(gui.state)
        self._create_select_actions(gui.state)
        self._create_view_actions(gui.state)

    def _create_edit_actions(self, state):
        w = 'edit'
        self.add(w, 'undo')
        self.add(w, 'redo')
        self.edit_actions.separator()

        # Clustering.
        self.add(w, 'merge', alias='g')
        self.add(w, 'split', alias='k')
        self.edit_actions.separator()

        # Move.
        self.add(w, 'move', prompt=True, n_args=2)
        for which in ('best', 'similar', 'all'):
            for group in ('noise', 'mua', 'good', 'unsorted'):
                self.add(w, 'move_%s_to_%s' % (which, group),
                         method_name='move',
                         method_args=(group, which),
                         submenu='Move to %s' % which,
                         docstring='Move %s to %s.' % (which, group))
        self.edit_actions.separator()

        # Label.
        self.add(w, 'label', alias='l', prompt=True, n_args=2)
        self.edit_actions.separator()

    def _create_select_actions(self, state):
        w = 'select'

        # Selection.
        self.add(w, 'select', alias='c', prompt=True, n_args=1)
        self.select_actions.separator()

        # Sort and filter
        self.add(w, 'filter', alias='f', prompt=True, n_args=1)
        self.add(w, 'sort', alias='s', prompt=True, n_args=1)

        # Sort by:
        for column in getattr(self.supervisor, 'columns', ()):
            self.add(
                w, 'sort_by_%s' % column.lower(), method_name='sort', method_args=(column,),
                docstring='Sort by %s' % column,
                submenu='Sort by', alias='s%s' % column.replace('_', '')[:2])

        self.select_actions.separator()

        self.add(w, 'reset_wizard')
        self.select_actions.separator()

        self.add(w, 'next')
        self.add(w, 'previous')
        self.select_actions.separator()

        self.add(w, 'next_best')
        self.add(w, 'previous_best')
        self.select_actions.separator()

    def _create_view_actions(self, state):
        w = 'view'
        cluster_labels_keys = getattr(self.supervisor, 'cluster_labels', {}).keys()
        cluster_metrics_keys = getattr(self.supervisor, 'cluster_metrics', {}).keys()

        # Change color field action.
        for field in chain(
                ('cluster', 'group', 'n_spikes'), cluster_labels_keys, cluster_metrics_keys):
            self.add(
                w, name='color_field_%s' % field.lower(),
                method_name='change_color_field',
                method_args=(field,),
                docstring='Change color field to %s' % field,
                alias='cf%s' % field.replace('_', '')[:2],
                submenu='Change color field')

        # Change color map action.
        for colormap in ('categorical', 'linear', 'diverging', 'rainbow'):
            self.add(
                w, name='colormap_%s' % colormap.lower(),
                method_name='change_colormap',
                method_args=(colormap,),
                docstring='Change colormap to %s' % colormap,
                alias='cm%s' % colormap[:2],
                submenu='Change colormap')

        # Change colormap categorical or continous.
        categorical = state.get('color_selector', Bunch()).get('categorical', None)
        self.add(w, 'toggle_categorical_colormap', checkable=True, checked=categorical is True)

        # Change colormap logarithmic.
        logarithmic = state.get('color_selector', Bunch()).get('logarithmic', None)
        self.add(w, 'toggle_logarithmic_colormap', checkable=True, checked=logarithmic is True)

        self.view_actions.separator()


# -----------------------------------------------------------------------------
# Clustering GUI component
# -----------------------------------------------------------------------------

def _is_group_masked(group):
    return group in ('noise', 'mua')


class Supervisor(object):
    """Component that brings manual clustering facilities to a GUI:

    * Clustering instance: merge, split, undo, redo
    * ClusterMeta instance: change cluster metadata (e.g. group)
    * Selection
    * Many manual clustering-related actions, snippets, shortcuts, etc.

    Parameters
    ----------

    spike_clusters : ndarray
    cluster_groups : dictionary {cluster_id: group_name}
    cluster_metrics : dictionary {metrics_name: function cluster_id => value}
    similarity: function cluster_id => [(cl, sim), ...]
    new_cluster_id: function that returns a brand new cluster id
    sort: initial sort (field_name, asc|desc)
    context: Context instance

    Events
    ------

    When this component is attached to a GUI, the following events are emitted:

    * select(cluster_ids)
    * cluster(up)
    * attach_gui(gui)
    * request_split()
    * error(msg)
    * color_mapping_changed()
    * save_clustering(spike_clusters, cluster_groups, *cluster_labels)

    """

    def __init__(
            self, spike_clusters=None, cluster_groups=None, cluster_metrics=None,
            cluster_labels=None, similarity=None, new_cluster_id=None, sort=None, context=None):
        super(Supervisor, self).__init__()
        self.context = context
        self.similarity = similarity  # function cluster => [(cl, sim), ...]
        self.actions = None  # will be set when attaching the GUI
        self._is_dirty = None
        self._sort = sort  # Initial sort requested in the constructor

        # Cluster metrics.
        # This is a dict {name: func cluster_id => value}.
        self.cluster_metrics = cluster_metrics or {}
        self.cluster_metrics['n_spikes'] = self.n_spikes

        # Cluster labels.
        # This is a dict {name: {cl: value}}
        self.cluster_labels = cluster_labels or {}

        self.columns = ['id']  # n_spikes comes from cluster_metrics
        self.columns += list(self.cluster_metrics.keys())
        self.columns += [
            label for label in self.cluster_labels.keys()
            if label not in self.columns + ['group']]

        # Create Clustering and ClusterMeta.
        # Load the cached spikes_per_cluster array.
        spc = context.load('spikes_per_cluster') if context else None
        self.clustering = Clustering(
            spike_clusters, spikes_per_cluster=spc, new_cluster_id=new_cluster_id)

        # Cache the spikes_per_cluster array.
        self._save_spikes_per_cluster()

        # Create the ClusterMeta instance.
        self.cluster_meta = create_cluster_meta(cluster_groups or {})
        # Add the labels.
        for label, values in self.cluster_labels.items():
            if label == 'group':
                continue
            self.cluster_meta.add_field(label)
            for cl, v in values.items():
                self.cluster_meta.set(label, [cl], v, add_to_stack=False)

        # Create the GlobalHistory instance.
        self._global_history = GlobalHistory(process_ups=_process_ups)

        # Create The Action Creator instance.
        self.action_creator = ActionCreator(self)
        connect(self._on_action, event='action', sender=self.action_creator)

        # Log the actions.
        connect(self._log_action, event='cluster', sender=self.clustering)
        connect(self._log_action_meta, event='cluster', sender=self.cluster_meta)

        # Raise supervisor.cluster
        @connect(sender=self.clustering)
        def on_cluster(sender, up):
            # NOTE: update the cluster meta of new clusters, depending on the values of the
            # ancestor clusters. In case of a conflict between the values of the old clusters,
            # the largest cluster wins and its value is set to its descendants.
            if up.added:
                self.cluster_meta.set_from_descendants(
                    up.descendants, largest_old_cluster=up.largest_old_cluster)
            emit('cluster', self, up)

        @connect(sender=self.cluster_meta)  # noqa
        def on_cluster(sender, up):
            emit('cluster', self, up)

        connect(self._save_new_cluster_id, event='cluster', sender=self)

        self._is_busy = False

    # Internal methods
    # -------------------------------------------------------------------------

    def _save_spikes_per_cluster(self):
        """Cache on the disk the dictionary with the spikes belonging to each cluster."""
        if not self.context:
            return
        self.context.save('spikes_per_cluster', self.clustering.spikes_per_cluster, kind='pickle')

    def _log_action(self, sender, up):
        """Log the clustering action (merge, split)."""
        if sender != self.clustering:
            return
        if up.history:
            logger.info(up.history.title() + " cluster assign.")
        elif up.description == 'merge':
            logger.info("Merge clusters %s to %s.", ', '.join(map(str, up.deleted)), up.added[0])
        else:
            logger.info("Assigned %s spikes.", len(up.spike_ids))

    def _log_action_meta(self, sender, up):
        """Log the cluster meta action (move, label)."""
        if sender != self.cluster_meta:
            return
        if up.history:
            logger.info(up.history.title() + " move.")
        else:
            logger.info(
                "Change %s for clusters %s to %s.", up.description,
                ', '.join(map(str, up.metadata_changed)), up.metadata_value)

        # Skip cluster metadata other than groups.
        if up.description != 'metadata_group':
            return

    def _save_new_cluster_id(self, sender, up):
        """Save the new cluster id on disk, knowing that cluster ids are unique for
        easier cache consistency."""
        new_cluster_id = self.clustering.new_cluster_id()
        if self.context:
            logger.debug("Save the new cluster id: %d.", new_cluster_id)
            self.context.save('new_cluster_id', dict(new_cluster_id=new_cluster_id))

    def _save_gui_state(self, gui):
        """Save the GUI state with the cluster view and similarity view."""
        b = Barrier()
        self.cluster_view.get_state(b(1))
        b.wait()
        state = b.result(1)[0][0]
        gui.state.update_view_state(self.cluster_view, state)

    def n_spikes(self, cluster_id):
        """Number of spikes in a given cluster."""
        return len(self.clustering.spikes_per_cluster.get(cluster_id, []))

    def _get_similar_clusters(self, sender, cluster_id):
        """Return the clusters similar to a given cluster."""
        sim = self.similarity(cluster_id)
        # Only keep existing clusters.
        clusters_set = set(self.clustering.cluster_ids)
        data = [dict(similarity='%.3f' % s, **self._get_cluster_info(c))
                for c, s in sim if c in clusters_set]
        return data

    def _get_cluster_info(self, cluster_id, exclude=()):
        """Return the data associated to a given cluster."""
        out = {'id': cluster_id,
               }
        for key, func in self.cluster_metrics.items():
            out[key] = func(cluster_id)
        for key in self.cluster_meta.fields:
            # includes group
            out[key] = self.cluster_meta.get(key, cluster_id)
        out['is_masked'] = _is_group_masked(out.get('group', None))
        return {k: v for k, v in out.items() if k not in exclude}

    @property
    def cluster_info(self):
        """The cluster view table as a list of per-cluster dictionaries."""
        return [self._get_cluster_info(cluster_id) for cluster_id in self.clustering.cluster_ids]

    def _create_views(self, gui=None, sort=None):
        """Create the cluster view and similarity view."""

        sort = sort or self._sort  # comes from either the GUI state or constructor

        # Create the cluster view.
        self.cluster_view = ClusterView(
            gui, data=self.cluster_info, columns=self.columns, sort=sort)
        # Update the action flow and similarity view when selection changes.
        connect(self._clusters_selected, event='select', sender=self.cluster_view)

        # Create the similarity view.
        self.similarity_view = SimilarityView(
            gui, columns=self.columns + ['similarity'], sort=('similarity', 'desc'))
        connect(self._get_similar_clusters, event='request_similar_clusters',
                sender=self.similarity_view)
        connect(self._similar_selected, event='select', sender=self.similarity_view)

        # Change the state after every clustering action, according to the action flow.
        connect(self._after_action, event='cluster', sender=self)

    def _reset_cluster_view(self):
        """Recreate the cluster view."""
        logger.debug("Reset the cluster view.")
        self.cluster_view._reset_table(
            data=self.cluster_info, columns=self.columns, sort=self._sort)

    def _clusters_added(self, cluster_ids):
        """Update the cluster and similarity views when new clusters are created."""
        logger.log(5, "Clusters added: %s", cluster_ids)
        data = [self._get_cluster_info(cluster_id) for cluster_id in cluster_ids]
        self.cluster_view.add(data)
        self.similarity_view.add(data)

    def _clusters_removed(self, cluster_ids):
        """Update the cluster and similarity views when clusters are removed."""
        logger.log(5, "Clusters removed: %s", cluster_ids)
        self.cluster_view.remove(cluster_ids)
        self.similarity_view.remove(cluster_ids)

    def _cluster_metadata_changed(self, field, cluster_ids, value):
        """Update the cluster and similarity views when clusters metadata is updated."""
        logger.log(5, "%s changed for %s to %s", field, cluster_ids, value)
        data = [{'id': cluster_id, field: value} for cluster_id in cluster_ids]
        for _ in data:
            _['is_masked'] = _is_group_masked(_.get('group', None))
        self.cluster_view.change(data)
        self.similarity_view.change(data)

    def _clusters_selected(self, sender, obj, **kwargs):
        """When clusters are selected in the cluster view, register the action in the history
        stack, update the similarity view, and emit the global supervisor.select event unless
        update_views is False."""
        if sender != self.cluster_view:
            return
        cluster_ids = obj['selected']
        next_cluster = obj['next']
        kwargs = obj.get('kwargs', {})
        logger.debug("Clusters selected: %s (%s)", cluster_ids, next_cluster)
        self.task_logger.log(self.cluster_view, 'select', cluster_ids, output=obj)
        # Update the similarity view when the cluster view selection changes.
        self.similarity_view.reset(cluster_ids)
        self.similarity_view.set_selected_index_offset(len(self.selected_clusters))
        # Emit supervisor.select event unless update_views is False. This happens after
        # a merge event, where the views should not be updated after the first cluster_view.select
        # event, but instead after the second similarity_view.select event.
        if kwargs.get('update_views', True):
            emit('select', self, self.selected, **kwargs)

    def _similar_selected(self, sender, obj):
        """When clusters are selected in the similarity view, register the action in the history
        stack, and emit the global supervisor.select event."""
        if sender != self.similarity_view:
            return
        similar = obj['selected']
        next_similar = obj['next']
        kwargs = obj.get('kwargs', {})
        logger.debug("Similar clusters selected: %s (%s)", similar, next_similar)
        self.task_logger.log(self.similarity_view, 'select', similar, output=obj)
        emit('select', self, self.selected, **kwargs)

    def _on_action(self, sender, name, *args):
        """Called when an action is triggered: enqueue and process the task."""
        assert sender == self.action_creator
        # The GUI should not be busy when calling a new action.
        if 'select' not in name and self._is_busy:
            logger.log(5, "The GUI is busy, waiting before calling the action.")
            _block(lambda: not self._is_busy)
        # Enqueue the requested action.
        self.task_logger.enqueue(self, name, *args)
        # Perform the action (which calls self.<name>(...)).
        self.task_logger.process()

    def _after_action(self, sender, up):
        """Called after an action: update the cluster and similarity views and update
        the selection."""
        # This is called once the action has completed. We update the tables.
        # Update the views with the old and new clusters.
        self._clusters_added(up.added)
        self._clusters_removed(up.deleted)
        self._cluster_metadata_changed(
            up.description.replace('metadata_', ''), up.metadata_changed, up.metadata_value)
        # After the action has finished, we process the pending actions,
        # like selection of new clusters in the tables.
        self.task_logger.process()

    @property
    def state(self):
        """GUI state, with the cluster view and similarity view states."""
        b = Barrier()
        self.cluster_view.get_state(b(1))
        self.similarity_view.get_state(b(2))
        b.wait()
        sc = b.result(1)[0][0]
        ss = b.result(2)[0][0]
        return Bunch({'cluster_view': Bunch(sc), 'similarity_view': Bunch(ss)})

    def _set_busy(self, busy):
        # If busy is the same, do nothing.
        if busy is self._is_busy:
            return
        self._is_busy = busy
        # Set the busy cursor.
        logger.log(5, "GUI is %sbusy" % ('' if busy else 'not '))
        set_busy(busy)
        # Let the cluster views know that the GUI is busy.
        self.cluster_view.set_busy(busy)
        self.similarity_view.set_busy(busy)

    def attach(self, gui):
        """Attach to the GUI."""

        # Create the cluster view and similarity view.
        self._create_views(
            gui=gui, sort=gui.state.get('ClusterView', {}).get('current_sort', None))

        # Create the TaskLogger.
        self.task_logger = TaskLogger(
            cluster_view=self.cluster_view,
            similarity_view=self.similarity_view,
            supervisor=self,
        )

        connect(self._save_gui_state, event='close', sender=gui)
        gui.add_view(self.cluster_view, position='left', closable=False)
        gui.add_view(self.similarity_view, position='left', closable=False)

        # Create the ClusterColorSelector instance.
        # Pass the state variables: color_field, colormap, categorical, logarithmic
        self.color_selector = ClusterColorSelector(
            cluster_meta=self.cluster_meta,
            cluster_metrics=self.cluster_metrics,
            cluster_ids=self.clustering.cluster_ids,
            **gui.state.get('color_selector', Bunch())
        )

        # Create all supervisor actions (edit and view menu).
        self.action_creator.attach(gui)
        self.actions = self.action_creator.edit_actions  # clustering actions
        self.select_actions = self.action_creator.select_actions
        self.view_actions = self.action_creator.view_actions
        emit('attach_gui', self)

        @connect(sender=self)
        def on_cluster(sender, up):
            # After a clustering action, get the cluster ids as shown
            # in the cluster view, and update the color selector accordingly.
            @self.cluster_view.get_ids
            def _update(cluster_ids):
                self.color_selector.set_cluster_ids(cluster_ids)

        # Call supervisor.save() when the save/ctrl+s action is triggered in the GUI.
        @connect(sender=gui)
        def on_request_save(sender):
            self.save()

        # Set the debouncer.
        self._busy = {}
        self._is_busy = False
        # Collect all busy events from the views, and sets the GUI as busy
        # if at least one view is busy.
        @connect
        def on_is_busy(sender, is_busy):
            self._busy[sender] = is_busy
            self._set_busy(any(self._busy.values()))

        @connect(sender=gui)
        def on_close(e):
            gui.state['color_selector'] = self.color_selector.state
            unconnect(on_is_busy)

    # Selection actions
    # -------------------------------------------------------------------------

    def select(self, *cluster_ids, callback=None):
        """Select a list of clusters."""
        # HACK: allow for `select(1, 2, 3)` in addition to `select([1, 2, 3])`
        # This makes it more convenient to select multiple clusters with
        # the snippet: `:c 1 2 3` instead of `:c 1,2,3`.
        if cluster_ids and isinstance(cluster_ids[0], (tuple, list)):
            cluster_ids = list(cluster_ids[0]) + list(cluster_ids[1:])
        # Remove non-existing clusters from the selection.
        #cluster_ids = self._keep_existing_clusters(cluster_ids)
        # Update the cluster view selection.
        self.cluster_view.select(cluster_ids, callback=callback)

    # Cluster view actions
    # -------------------------------------------------------------------------

    def sort(self, column, sort_dir='desc'):
        """Sort the cluster view by a given column, in a given order (asc or desc)."""
        self.cluster_view.sort_by(column, sort_dir=sort_dir)

    def filter(self, text):
        """Filter the clusters using a Javascript expression on the column names."""
        self.cluster_view.filter(text)

    # Clustering actions
    # -------------------------------------------------------------------------

    @property
    def selected_clusters(self):
        """Selected clusters in the cluster view only."""
        state = self.task_logger.last_state()
        return state[0] or [] if state else []

    @property
    def selected_similar(self):
        """Selected clusters in the similarity view only."""
        state = self.task_logger.last_state()
        return state[2] or [] if state else []

    @property
    def selected(self):
        """Selected clusters in the cluster and similarity views."""
        return _uniq(self.selected_clusters + self.selected_similar)

    def merge(self, cluster_ids=None, to=None):
        """Merge the selected clusters."""
        if cluster_ids is None:
            cluster_ids = self.selected
        if len(cluster_ids or []) <= 1:
            return
        out = self.clustering.merge(cluster_ids, to=to)
        self._global_history.action(self.clustering)
        return out

    def split(self, spike_ids=None, spike_clusters_rel=0):
        """Make a new cluster out of the specified spikes."""
        if spike_ids is None:
            # Concatenate all spike_ids returned by views who respond to request_split.
            spike_ids = emit('request_split', self)
            spike_ids = np.concatenate(spike_ids).astype(np.int64)
            assert spike_ids.dtype == np.int64
            assert spike_ids.ndim == 1
        if len(spike_ids) == 0:
            msg = ("You first need to select spikes in the feature "
                   "view with a few Ctrl+Click around the spikes "
                   "that you want to split.")
            emit('error', self, msg)
            return
        out = self.clustering.split(
            spike_ids, spike_clusters_rel=spike_clusters_rel)
        self._global_history.action(self.clustering)
        return out

    # Move actions
    # -------------------------------------------------------------------------

    @property
    def fields(self):
        """List of all cluster label names."""
        return tuple(f for f in self.cluster_meta.fields if f not in ('group',))

    def get_labels(self, field):
        """Return the labels of all clusters, for a given label name."""
        return {c: self.cluster_meta.get(field, c)
                for c in self.clustering.cluster_ids}

    def label(self, name, value, cluster_ids=None):
        """Assign a label to some clusters."""
        if cluster_ids is None:
            cluster_ids = self.selected
        if not hasattr(cluster_ids, '__len__'):
            cluster_ids = [cluster_ids]
        if len(cluster_ids) == 0:
            return
        self.cluster_meta.set(name, cluster_ids, value)
        self._global_history.action(self.cluster_meta)
        # Add column if needed.
        if name != 'group' and name not in self.columns:
            logger.debug("Add column %s.", name)
            self.columns.append(name)
            self._reset_cluster_view()

    def move(self, group, which):
        """Assign a cluster group to some clusters."""
        if which == 'all':
            which = self.selected
        elif which == 'best':
            which = self.selected_clusters
        elif which == 'similar':
            which = self.selected_similar
        if isinstance(which, int):
            which = [which]
        if not which:
            return
        _ensure_all_ints(which)
        logger.debug("Move %s to %s.", which, group)
        group = 'unsorted' if group is None else group
        self.label('group', group, cluster_ids=which)

    # Wizard actions
    # -------------------------------------------------------------------------

    # There are callbacks because these functions call Javascript functions that return
    # asynchronously in Qt5.

    def reset_wizard(self, callback=None):
        """Reset the wizard."""
        self.cluster_view.first(callback=callback or partial(emit, 'wizard_done', self))

    def next_best(self, callback=None):
        """Select the next best cluster in the cluster view."""
        self.cluster_view.next(callback=callback or partial(emit, 'wizard_done', self))

    def previous_best(self, callback=None):
        """Select the previous best cluster in the cluster view."""
        self.cluster_view.previous(callback=callback or partial(emit, 'wizard_done', self))

    def next(self, callback=None):
        """Select the next cluster in the similarity view."""
        state = self.task_logger.last_state()
        if not state or not state[0]:
            self.cluster_view.first(callback=callback or partial(emit, 'wizard_done', self))
        else:
            self.similarity_view.next(callback=callback or partial(emit, 'wizard_done', self))

    def previous(self, callback=None):
        """Select the previous cluster in the similarity view."""
        self.similarity_view.previous(callback=callback or partial(emit, 'wizard_done', self))

    # Color mapping actions
    # -------------------------------------------------------------------------

    def change_color_field(self, color_field):
        """Change the color field (the name of the cluster view column used for the selected
        colormap)."""
        self.color_selector.set_color_mapping(color_field=color_field)
        emit('color_mapping_changed', self)

    def change_colormap(self, colormap):
        """Change the colormap."""
        self.color_selector.set_color_mapping(colormap=colormap)
        emit('color_mapping_changed', self)

    def toggle_categorical_colormap(self, checked):
        """Use a categorical or continuous colormap."""
        self.color_selector.set_color_mapping(categorical=checked)
        emit('color_mapping_changed', self)

    def toggle_logarithmic_colormap(self, checked):
        """Use a logarithmic transform or not for the colormap."""
        self.color_selector.set_color_mapping(logarithmic=checked)
        emit('color_mapping_changed', self)

    # Other actions
    # -------------------------------------------------------------------------

    def is_dirty(self):
        """Return whether there are any pending changes."""
        return self._is_dirty if self._is_dirty in (False, True) else len(self._global_history) > 1

    def undo(self):
        """Undo the last action."""
        self._global_history.undo()

    def redo(self):
        """Undo the last undone action."""
        self._global_history.redo()

    def save(self):
        """Save the manual clustering back to disk.

        This method emits the `save_clustering(spike_clusters, groups, *labels)` event.
        It is up to the caller to react to this event and save the data to disk.

        """
        spike_clusters = self.clustering.spike_clusters
        groups = {c: self.cluster_meta.get('group', c) or 'unsorted'
                  for c in self.clustering.cluster_ids}
        # List of tuples (field_name, dictionary).
        labels = [(field, self.get_labels(field))
                  for field in self.cluster_meta.fields
                  if field not in ('next_cluster')]
        emit('save_clustering', self, spike_clusters, groups, *labels)
        # Cache the spikes_per_cluster array.
        self._save_spikes_per_cluster()
        self._is_dirty = False

    def block(self):
        """Block until there are no pending actions.

        Only used in the automated testing suite.

        """
        _block(lambda: self.task_logger.has_finished() and not self._is_busy)
        assert not self._is_busy
        _wait(50)
