import os
import logging
import numpy as np
from enum import Enum, unique
from contextlib import suppress
from .model import MainWindowModel
from sscanss.ui.commands import (InsertPrimitive, DeleteSample, MergeSample,
                                 InsertSampleFromFile, RotateSample, TranslateSample, TransformSample,
                                 ChangeMainSample, InsertPointsFromFile, InsertPoints, DeletePoints,
                                 MovePoints, EditPoints, InsertVectorsFromFile, InsertVectors, LockJoint,
                                 IgnoreJointLimits, MovePositioner, ChangePositioningStack, ChangePositionerBase,
                                 ChangeCollimator, ChangeJawAperture, InsertAlignmentMatrix)
from sscanss.core.io import read_trans_matrix, read_fpos
from sscanss.core.util import TransformType, MessageSeverity, Worker, toggleActionInGroup, PointType
from sscanss.core.math import matrix_from_pose, find_3d_correspondence, rigid_transform


@unique
class MessageReplyType(Enum):
    Save = 1
    Discard = 2
    Cancel = 3


class MainWindowPresenter:
    def __init__(self, view):
        self.view = view
        self.model = MainWindowModel()
        self.worker = None

        self.recent_list_size = 10  # Maximum size of the recent project list

    def notifyError(self, message):
        logging.exception(message)
        self.view.showMessage(message)

    def useWorker(self, func, args, on_success=None, on_failure=None, on_complete=None):
        self.worker = Worker.callFromWorker(func, args, on_success, on_failure, on_complete)

    def createProject(self, name, instrument):
        """
        This function creates the stub data for the project

        :param name: The name of the project
        :type name: str
        :param instrument: The name of the instrument used for the project
        :type instrument: str
        """
        self.view.scenes.reset()
        self.resetSimulation()
        self.model.createProjectData(name, instrument)
        self.model.save_path = ''
        self.view.undo_stack.clear()

    def updateView(self):
        self.view.showProjectName()
        toggleActionInGroup(self.model.instrument.name, self.view.change_instrument_action_group)
        self.view.resetInstrumentMenu()
        detector_count = len(self.model.instrument.detectors)
        for name, detector in self.model.instrument.detectors.items():
            show_more = detector.positioner is not None
            title = 'Detector' if detector_count == 1 else f'{name} Detector'
            collimator_name = None if detector.current_collimator is None else detector.current_collimator.name
            self.view.addCollimatorMenu(name, detector.collimators.keys(), collimator_name, title, show_more)

        self.view.docks.closeAll()
        self.view.updateMenus()

    def projectCreationError(self, exception, args):
        msg = 'An error occurred while parsing the instrument description file for {}.\n\n' \
              'Please contact the maintainer of the instrument model.'.format(args[-1])

        logging.error(msg, exc_info=exception)
        self.view.showMessage(msg)

    def saveProject(self, save_as=False):
        """
        This function saves a project to a file. A file dialog will be opened for the first save
        after which the function will save to the same location. if save_as id True a dialog is
        opened every time

        :param save_as: A flag denoting whether to use file dialog or not
        :type save_as: bool
        """

        # Avoids saving when there are no changes
        if self.view.undo_stack.isClean() and self.model.save_path and not save_as:
            return

        filename = self.model.save_path
        if save_as or not filename:
            filename = self.view.showSaveDialog('hdf5 File (*.h5)', current_dir=filename, title='Save Project')
            if not filename:
                return

        try:
            self.model.saveProjectData(filename)
            self.updateRecentProjects(filename)
            self.model.save_path = filename
            self.view.undo_stack.setClean()
        except OSError:
            self.notifyError(f'An error occurred while attempting to save this project ({filename})')

    def openProject(self, filename):
        self.resetSimulation()
        self.model.loadProjectData(filename)
        self.updateRecentProjects(filename)
        self.model.save_path = filename
        self.view.undo_stack.clear()
        self.view.undo_stack.setClean()

    def projectOpenError(self, exception, args):
        filename = args[0]
        if isinstance(exception, ValueError):
            msg = f'{filename} could not open because it has incorrect data.'
        elif isinstance(exception, (KeyError, AttributeError)):
            msg = f'{filename} could not open because it has an incorrect format.'
        elif isinstance(exception, OSError):
            msg = 'An error occurred while opening this file.\nPlease check that ' \
                  f'the file exist and also that this user has access privileges for this file.\n({filename})'
        else:
            msg = f'An unknown error occurred while opening {filename}.'

        logging.error(msg, exc_info=exception)
        self.view.showMessage(msg)

    def confirmSave(self):
        """
        Checks if the project is saved and asks the user to save if necessary

        :return: True if the project is saved or user chose to discard changes
        :rtype: bool
        """
        if self.view.undo_stack.isClean():
            return True

        reply = self.view.showSaveDiscardMessage(self.model.project_data['name'])

        if reply == MessageReplyType.Save:
            if self.model.save_path:
                self.saveProject()
                return True
            else:
                self.saveProject(save_as=True)
                return True if self.view.undo_stack.isClean() else False

        elif reply == MessageReplyType.Discard:
            return True
        else:
            return False

    def updateRecentProjects(self, filename):
        """
        This function adds a filename entry to the front of the recent projects list
        if it does not exist in the list. if the entry already exist, it is moved to the
        front but not duplicated.

        :param filename: project path to add to recents lists
        :type filename: str
        """
        projects = self.view.recent_projects
        projects.insert(0, filename)
        projects = list(dict.fromkeys(projects))
        if len(projects) <= self.recent_list_size:
            self.view.recent_projects = projects
        else:
            self.view.recent_projects = projects[:self.recent_list_size]

    def importSample(self):
        filename = self.view.showOpenDialog('3D Files (*.stl *.obj)',
                                            title='Import Sample Model',
                                            current_dir=self.model.save_path)

        if not filename:
            return

        insert_command = InsertSampleFromFile(filename, self, self.confirmCombineSample())
        self.view.undo_stack.push(insert_command)

    def exportSamples(self):
        if not self.model.sample:
            self.view.showMessage('No samples have been added to the project', MessageSeverity.Information)
            return

        if len(self.model.sample) > 1:
            sample_key = self.view.showSampleExport(self.model.sample.keys())
        else:
            sample_key = list(self.model.sample.keys())[0]

        if not sample_key:
            return

        filename = self.view.showSaveDialog('Binary STL File(*.stl)', title=f'Export {sample_key}',
                                            current_dir=self.model.save_path)

        if not filename:
            return

        try:
            self.model.saveSample(filename, sample_key)
        except (IOError, ValueError):
           self.notifyError(f'An error occurred while exporting the sample ({sample_key}) to {filename}.')

    def addPrimitive(self, primitive, args):
        insert_command = InsertPrimitive(primitive, args, self, combine=self.confirmCombineSample())
        self.view.undo_stack.push(insert_command)
        self.view.docks.showSampleManager()

    def transformSample(self, angles_or_offset, sample_key, transform_type):
        if transform_type == TransformType.Rotate:
            transform_command = RotateSample(angles_or_offset, sample_key, self)
        elif transform_type == TransformType.Translate:
            transform_command = TranslateSample(angles_or_offset, sample_key, self)
        else:
            transform_command = TransformSample(angles_or_offset, sample_key, self)

        self.view.undo_stack.push(transform_command)

    def deleteSample(self, sample_key):
        delete_command = DeleteSample(sample_key, self)
        self.view.undo_stack.push(delete_command)

    def mergeSample(self, sample_key):
        merge_command = MergeSample(sample_key, self)
        self.view.undo_stack.push(merge_command)

    def changeMainSample(self, sample_key):
        change_main_command = ChangeMainSample(sample_key, self)
        self.view.undo_stack.push(change_main_command)

    def confirmCombineSample(self):
        if self.model.sample:
            question = 'A sample model has already been added to the project.\n\n' \
                       'Do you want replace the model or combine them?'
            choice = self.view.showSelectChoiceMessage(question, ['Combine', 'Replace'], default_choice=1)

            if choice == 'Combine':
                return True

        return False

    def confirmClearStack(self):
        if self.view.undo_stack.count() == 0:
            return True

        question = 'This action cannot be undone, the undo history will be cleared.\n\n' \
                   'Do you want proceed with this action?'
        choice = self.view.showSelectChoiceMessage(question, ['Proceed', 'Cancel'], default_choice=1)

        if choice == 'Proceed':
            return True

        return False

    def importPoints(self, point_type):
        if not self.model.sample:
            self.view.showMessage('A sample model should be added before {} points'.format(point_type.value.lower()),
                                  MessageSeverity.Information)
            return

        filename = self.view.showOpenDialog(f'{point_type.value} File(*.{point_type.value.lower()})',
                                            title=f'Import {point_type.value} Points',
                                            current_dir=self.model.save_path)

        if not filename:
            return

        insert_command = InsertPointsFromFile(filename, point_type, self)
        self.view.undo_stack.push(insert_command)

    def exportPoints(self, point_type):
        points = self.model.fiducials if point_type == PointType.Fiducial else self.model.measurement_points
        if points.size == 0:
            self.view.showMessage('No {} points have been added to the project'.format(point_type.value.lower()),
                                  MessageSeverity.Information)
            return

        filename = self.view.showSaveDialog(f'{point_type.value} File(*.{point_type.value.lower()})',
                                            title=f'Export {point_type.value} Points',
                                            current_dir=self.model.save_path)

        if not filename:
            return

        try:
            self.model.savePoints(filename, point_type)
        except (IOError, ValueError):
            self.notifyError(f'An error occurred while exporting the {point_type.value} points to {filename}.')

    def addPoints(self, points, point_type, show_manager=True):
        if not self.model.sample:
            self.view.showMessage('A sample model should be added before {} points'.format(point_type.value.lower()),
                                  MessageSeverity.Information)
            return

        insert_command = InsertPoints(points, point_type, self)
        self.view.undo_stack.push(insert_command)
        if show_manager:
            self.view.docks.showPointManager(point_type)

    def deletePoints(self, indices, point_type):
        delete_command = DeletePoints(indices, point_type, self)
        self.view.undo_stack.push(delete_command)

    def movePoints(self, move_from, move_to, point_type):
        move_command = MovePoints(move_from, move_to, point_type, self)
        self.view.undo_stack.push(move_command)

    def editPoints(self, values, point_type):
        edit_command = EditPoints(values, point_type, self)
        self.view.undo_stack.push(edit_command)

    def importVectors(self):
        if not self.model.sample:
            self.view.showMessage('Sample model and measurement points should be added before vectors',
                                  MessageSeverity.Information)
            return

        if self.model.measurement_points.size == 0:
            self.view.showMessage('Measurement points should be added before vectors', MessageSeverity.Information)
            return

        filename = self.view.showOpenDialog('Measurement Vector File(*.vecs)',
                                            title='Import Measurement Vectors',
                                            current_dir=self.model.save_path)

        if not filename:
            return

        insert_command = InsertVectorsFromFile(filename, self)
        self.view.undo_stack.push(insert_command)

    def addVectors(self, point_index, strain_component, alignment, detector, key_in=None, reverse=False):
        if not self.model.sample:
            self.view.showMessage('Sample model and measurement points should be added before vectors',
                                  MessageSeverity.Information)
            return

        if self.model.measurement_points.size == 0:
            self.view.showMessage('Measurement points should be added before vectors', MessageSeverity.Information)
            return

        insert_command = InsertVectors(self, point_index, strain_component, alignment, detector, key_in, reverse)
        self.view.undo_stack.push(insert_command)

    def exportVectors(self):
        if self.model.measurement_vectors.shape[0] == 0:
            self.view.showMessage('No measurement vectors have been added to the project', MessageSeverity.Information)
            return

        filename = self.view.showSaveDialog('Measurement Vector File(*.vecs)',
                                            title=f'Export Measurement Vectors',
                                            current_dir=self.model.save_path)

        if not filename:
            return

        try:
            self.model.saveVectors(filename)
        except (IOError, ValueError):
            self.notifyError(f'An error occurred while exporting the measurement vector to {filename}.')

    def importTransformMatrix(self):
        filename = self.view.showOpenDialog('Transformation Matrix File(*.trans)',
                                            title='Import Transformation Matrix',
                                            current_dir=self.model.save_path)

        if not filename:
            return None

        try:
            return read_trans_matrix(filename)
        except (IOError, ValueError):
            msg = 'An error occurred while reading the .trans file ({}).\nPlease check that ' \
                  'the file has the correct format.\n'

            self.notifyError(msg.format(filename))

        return None

    def exportAlignmentMatrix(self):
        if self.model.alignment is None:
            self.view.showMessage('Sample has not been aligned on instrument.', MessageSeverity.Information)
            return

        filename = self.view.showSaveDialog('Transformation Matrix File(*.trans)',
                                            title=f'Export Alignment Matrix',
                                            current_dir=self.model.save_path)

        if not filename:
            return

        try:
            np.savetxt(filename, self.model.alignment, delimiter='\t', fmt='%.7f')
        except (IOError, ValueError):
            self.notifyError(f'An error occurred while exporting the alignment matrix to {filename}.')

    def changeCollimators(self, detector, collimator):
        command = ChangeCollimator(detector, collimator, self)
        self.view.undo_stack.push(command)

    def lockPositionerJoint(self, positioner_name, index, value):
        command = LockJoint(positioner_name, index, value, self)
        self.view.undo_stack.push(command)

    def ignorePositionerJointLimits(self, positioner_name, index, value):
        command = IgnoreJointLimits(positioner_name, index, value, self)
        self.view.undo_stack.push(command)

    def movePositioner(self, positioner_name, q):
        command = MovePositioner(positioner_name, q, self)
        self.view.undo_stack.push(command)

    def changePositioningStack(self, name):
        command = ChangePositioningStack(name, self)
        self.view.undo_stack.push(command)

    def changePositionerBase(self, positioner, matrix):
        command = ChangePositionerBase(positioner, matrix, self)
        self.view.undo_stack.push(command)

    def changeJawAperture(self, aperture):
        command = ChangeJawAperture(aperture, self)
        self.view.undo_stack.push(command)

    def changeInstrument(self, instrument):
        if self.model.instrument.name == instrument:
            return

        if not self.confirmClearStack():
            toggleActionInGroup(self.model.instrument.name, self.view.change_instrument_action_group)
            return

        self.view.progress_dialog.show(f'Loading {instrument} Instrument')
        self.useWorker(self._changeInstrumentHelper, [instrument], self.updateView,
                       self.projectCreationError, self.view.progress_dialog.close)

    def _changeInstrumentHelper(self, instrument):
        self.resetSimulation()
        self.model.changeInstrument(instrument)
        self.model.save_path = ''
        self.view.undo_stack.clear()
        self.view.undo_stack.resetClean()

    def alignSample(self, matrix):
        command = InsertAlignmentMatrix(matrix, self)
        self.view.undo_stack.push(command)

    def alignSampleWithPose(self, pose):
        if not self.model.sample:
            self.view.showMessage('A sample model should be added before alignment', MessageSeverity.Information)
            return
        self.alignSample(matrix_from_pose(pose))

    def alignSampleWithMatrix(self):
        if not self.model.sample:
            self.view.showMessage('A sample model should be added before alignment', MessageSeverity.Information)
            return

        matrix = self.importTransformMatrix()
        if matrix is None:
            return
        self.view.scenes.switchToInstrumentScene()
        self.alignSample(matrix)

    def alignSampleWithFiducialPoints(self):
        if not self.model.sample:
            self.view.showMessage('A sample model should be added before alignment', MessageSeverity.Information)
            return

        if self.model.fiducials.size < 3:
            self.view.showMessage('A minimum of 3 fiducial points is required for sample alignment.',
                                  MessageSeverity.Information)
            return

        filename = self.view.showOpenDialog('Alignment Fiducial File(*.fpos)',
                                            title='Import Sample Alignment Fiducials',
                                            current_dir=self.model.save_path)

        if not filename:
            return

        try:
            index, points, poses = read_fpos(filename)
        except (IOError, ValueError):
            msg = 'An error occurred while reading the .fpos file ({}).\nPlease check that ' \
                  'the file has the correct format.\n'

            self.notifyError(msg.format(filename))
            return

        if index.size < 3:
            self.view.showMessage('A minimum of 3 points is required for sample alignment.')
            return

        count = self.model.fiducials.size
        if np.any(index < 0):
            self.view.showMessage('Negative point indices are not allowed.')
            return
        elif np.any(index >= count):
            self.view.showMessage(f'Point index {index.max()+1} exceeds the number of fiducial points {count}.')
            return

        positioner = self.model.instrument.positioning_stack
        link_count = len(positioner.links)
        if poses.size != 0 and poses.shape[1] != link_count:
            self.view.showMessage(f'Incorrect number of joint offsets in fpos file, '
                                  f'got {poses.shape[1]} but expected {link_count}')
            return
        q = positioner.set_points

        if poses.size != 0:
            for i, pose in enumerate(poses):
                pose = positioner.fromUserFormat(pose)
                matrix = (positioner.fkine(pose, ignore_locks=True) @ positioner.tool_link).inverse()
                _matrix = matrix[0:3, 0:3].transpose()
                offset = matrix[0:3, 3].transpose()
                points[i, :] = points[i, :] @ _matrix + offset

            positioner.fkine(q, ignore_locks=True)

        enabled = self.model.fiducials[index].enabled
        result = self.rigidTransform(index, points, enabled)

        self.view.showAlignmentError()
        self.view.alignment_error.updateModel(index, enabled, points, result)

        with suppress(ValueError):
            new_index = find_3d_correspondence(self.model.fiducials.points, points)
            if np.any(new_index != index):
                self.view.alignment_error.indexOrder(new_index)

    def rigidTransform(self, index, points, enabled):
        reference = self.model.fiducials[index].points
        return rigid_transform(reference[enabled], points[enabled])

    def runSimulation(self):
        if self.model.alignment is None:
            self.view.showMessage('Sample must be aligned on the instrument for Simulation',
                                  MessageSeverity.Information)
            return

        if self.model.measurement_points.size == 0:
            self.view.showMessage('Measurement points should be added before Simulation', MessageSeverity.Information)
            return

        self.view.docks.showSimulationResults()
        # Start the simulation process. This can be slow due to pickling of arguments
        if self.model.simulation is not None and self.model.simulation.isRunning():
            return

        compute_path_length = self.view.compute_path_length_action.isChecked()
        render_graphics = self.view.show_sim_graphics_action.isChecked()
        check_limits = self.view.check_limits_action.isChecked()

        self.model.createSimulation(compute_path_length, render_graphics, check_limits)
        self.model.simulation.start()

    def stopSimulation(self):
        if self.model.simulation is None:
            return

        self.model.simulation.abort()

    def resetSimulation(self):
        self.stopSimulation()
        self.model.simulation = None

    def exportScript(self, script):
        save_path = self.model.save_path
        if save_path:
            name, _ = os.path.splitext(save_path)
            save_path = f'{name}_script'

        filename = self.view.showSaveDialog('Text File (*.txt)', current_dir=save_path, title='Export Script')
        if filename:
            script_text = script()
            try:
                with open(filename, "w", newline="\n") as text_file:
                    text_file.write(script_text)
                return True
            except OSError:
                self.notifyError(f'A error occurred while attempting to save this project ({filename})')

        return False
