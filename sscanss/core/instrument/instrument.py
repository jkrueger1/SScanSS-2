from enum import Enum, unique
import pystache
from .robotics import IKSolver
from ..scene.node import Node


class Instrument:
    """This class represents a diffractometer instrument

    :param name: name of instrument
    :type name: str
    :param gauge_volume: gauge volume of the instrument
    :type gauge_volume: Vector3
    :param detectors: detectors
    :type detectors: Dict[str, Detector]
    :param jaws: jaws
    :type jaws: Jaws
    :param positioners: positioners
    :type positioners: Dict[str, SerialManipulator]
    :param positioning_stacks: positioning stacks
    :type positioning_stacks: Dict[str, List[str]]
    :param script: template for instrument script
    :type script: Script
    :param fixed_hardware: mesh for fixed hardware
    :type fixed_hardware: Dict[str, Mesh]
    """
    def __init__(self, name, gauge_volume, detectors, jaws, positioners, positioning_stacks, script,
                 fixed_hardware):
        self.name = name
        self.gauge_volume = gauge_volume
        self.detectors = detectors
        self.positioners = positioners
        self.jaws = jaws
        self.fixed_hardware = fixed_hardware
        self.positioning_stacks = positioning_stacks
        self.script = script
        self.loadPositioningStack(list(self.positioning_stacks.keys())[0])

    @property
    def q_vectors(self):
        q_vectors = []
        beam_axis = -self.jaws.beam_direction
        for detector in self.detectors.values():
            vector = beam_axis + detector.diffracted_beam
            if vector.length > 0.0001:
                q_vectors.append(vector.normalized)
            else:
                q_vectors.append(vector)
        return q_vectors

    @property
    def beam_in_gauge_volume(self):
        # Check beam hits the gauge volume
        actual_axis = self.gauge_volume - self.jaws.beam_source
        axis = self.jaws.beam_direction ^ actual_axis
        if axis.length > 0.0001:
            return False

        return True

    def getPositioner(self, name):
        """ get positioner or positioning stack by name

        :param name: name of positioner or stack
        :type name: str
        :return: positioner or positioning stack
        :rtype: Union[SerialManipulator, PositioningStack]
        """
        if name == self.positioning_stack.name:
            return self.positioning_stack

        if name in self.positioners:
            return self.positioners[name]
        else:
            raise ValueError(f'"{name}" Positioner could not be found.')

    def loadPositioningStack(self, stack_key):
        """ load a positioning stack with the specified key

        :param stack_key: name of stack
        :type stack_key: str
        """
        positioner_keys = self.positioning_stacks[stack_key]

        for i in range(len(positioner_keys)):
            key = positioner_keys[i]
            if i == 0:
                self.positioning_stack = PositioningStack(stack_key, self.positioners[key])
            else:
                self.positioning_stack.addPositioner(self.positioners[key])


class Jaws:
    def __init__(self, name, beam_source, beam_direction, aperture, lower_limit, upper_limit, mesh,
                 positioner=None):
        self.name = name
        self.aperture = aperture
        self.initial_source = beam_source
        self.beam_source = beam_source
        self.initial_direction = beam_direction
        self.beam_direction = beam_direction
        self.aperture_lower_limit = lower_limit
        self.aperture_upper_limit = upper_limit
        self.positioner = positioner
        self.mesh = mesh

    def updateBeam(self):
        pose = self.positioner.pose
        self.beam_direction = pose[0:3, 0:3] @ self.initial_direction
        self.beam_source = pose[0:3, 0:3] @ self.initial_source + pose[0:3, 3]

    @property
    def positioner(self):
        return self._positioner

    @positioner.setter
    def positioner(self, value):
        self._positioner = value
        if value is not None:
            self._positioner.fkine = self.__wrapper(self._positioner.fkine)
            self.updateBeam()

    def __wrapper(self, func):
        def wrapped(*args, **kwargs):
            result = func(*args, **kwargs)
            self.updateBeam()
            return result
        return wrapped

    def model(self):
        if self.positioner is None:
            # This ensures similar representation (i.e. empty parent node with
            # children) whether the jaws has a positioner or not.
            node = Node()
            node.addChild(Node(self.mesh))
            return node
        else:
            node = self.positioner.model()
            transformed_mesh = self.mesh.transformed(self.positioner.pose)
            node.addChild(Node(transformed_mesh))
        return node


class Detector:
    def __init__(self, name, diffracted_beam, collimators=None, positioner=None):
        self.name = name
        self.__current_collimator = None
        self.initial_beam = diffracted_beam
        self.diffracted_beam = diffracted_beam
        self.collimators = {} if collimators is None else collimators
        self.positioner = positioner

    def updateBeam(self):
        self.diffracted_beam = self.positioner.pose[0:3, 0:3] @ self.initial_beam

    @property
    def positioner(self):
        return self._positioner

    @positioner.setter
    def positioner(self, value):
        self._positioner = value
        if value is not None:
            self._positioner.fkine = self.__wrapper(self._positioner.fkine)
            self.updateBeam()

    def __wrapper(self, func):
        def wrapped(*args, **kwargs):
            result = func(*args, **kwargs)
            self.updateBeam()
            return result

        return wrapped

    @property
    def current_collimator(self):
        return self.__current_collimator

    @current_collimator.setter
    def current_collimator(self, key):
        if key in self.collimators:
            self.__current_collimator = self.collimators[key]
        else:
            self.__current_collimator = None

    def model(self):
        if self.positioner is None:
            return Node() if self.current_collimator is None else self.current_collimator.model()
        else:
            node = self.positioner.model()
            if self.current_collimator is not None:
                transformed_mesh = self.current_collimator.mesh.transformed(self.positioner.pose)
                node.addChild(Node(transformed_mesh))
            return node


class Collimator:
    def __init__(self, name, aperture, mesh):
        self.name = name
        self.aperture = aperture
        self.mesh = mesh

    def model(self):
        node = Node()
        node.addChild(Node(self.mesh))
        return node


class PositioningStack:
    """ This class represents a group of serial manipulators stacked on each other.
    The stack has a fixed base manipulator and auxiliary manipulator can be appended to it.
     When an auxiliary is appended the fixed link btw the stack and the new is computed.
     more details - https://doi.org/10.1016/j.nima.2015.12.067

    :param name: name of stack
    :type name: str
    :param fixed: base manipulator
    :type fixed: sscanss.core.instrument.robotics.SerialManipulator
    """
    def __init__(self, name, fixed):

        self.name = name
        self.fixed = fixed
        self.fixed.reset()
        self.tool_link = self.fixed.pose.inverse()
        self.auxiliary = []
        self.link_matrix = []
        self.ik_solver = IKSolver(self)

    @property
    def tool_pose(self):
        return self.pose @ self.tool_link

    @property
    def pose(self):
        """ the pose of the end effector of the manipulator

        :return: transformation matrix
        :rtype: Matrix44
        """
        T = self.fixed.pose
        for link, positioner in zip(self.link_matrix, self.auxiliary):
            T @= link @ positioner.pose
        return T

    def __defaultPoseInverse(self, positioner):
        """ calculates the inverse of the default pose for the given positioner which
        is used to calculate the fixed link

        :param positioner: auxiliary positioner
        :type positioner: sscanss.core.instrument.robotics.SerialManipulator
        :return: transformation matrix
        :rtype: Matrix44
        """
        q = positioner.set_points
        positioner.resetOffsets()
        matrix = positioner.pose.inverse()
        positioner.fkine(q, ignore_locks=True)

        return matrix

    def changeBaseMatrix(self, positioner, matrix):
        """ change the base matrix of a positioner in the stack

        :param positioner: auxiliary positioner
        :type positioner: sscanss.core.instrument.robotics.SerialManipulator
        :param matrix: new base matrix
        :type matrix: Matrix44
        """
        index = self.auxiliary.index(positioner)
        positioner.base = matrix

        if positioner is not self.auxiliary[-1]:
            self.link_matrix[index+1] = self.__defaultPoseInverse(positioner)
        else:
            self.tool_link = self.__defaultPoseInverse(positioner)

    def addPositioner(self, positioner):
        """ append a positioner to the stack

        :param positioner: auxiliary positioner
        :type positioner: sscanss.core.instrument.robotics.SerialManipulator
        """
        positioner.reset()
        self.tool_link = positioner.pose.inverse()
        last_positioner = self.auxiliary[-1] if self.auxiliary else self.fixed
        self.auxiliary.append(positioner)
        self.link_matrix.append(self.__defaultPoseInverse(last_positioner))

    @property
    def configuration(self):
        """ current configuration (joint offsets for all links) of the stack

        :return: current configuration
        :rtype: list[float]
        """
        conf = []
        conf.extend(self.fixed.configuration)
        for positioner in self.auxiliary:
            conf.extend(positioner.configuration)

        return conf

    @property
    def links(self):
        """ links from all manipulators the stack

        :return: links in stack
        :rtype: list[sscanss.core.instrument.robotics.Link]
        """
        links = []
        links.extend(self.fixed.links)
        for positioner in self.auxiliary:
            links.extend(positioner.links)

        return links

    def fromUserFormat(self, q):
        start, end = 0, self.fixed.numberOfLinks
        conf = self.fixed.fromUserFormat(q[start:end])
        for positioner in self.auxiliary:
            start, end = end, end + positioner.numberOfLinks
            conf.extend(positioner.fromUserFormat(q[start:end]))

        return conf

    def toUserFormat(self, q):
        start, end = 0, self.fixed.numberOfLinks
        conf = self.fixed.toUserFormat(q[start:end])
        for positioner in self.auxiliary:
            start, end = end, end + positioner.numberOfLinks
            conf.extend(positioner.toUserFormat(q[start:end]))

        return conf

    @property
    def order(self):
        end = self.fixed.numberOfLinks
        order = self.fixed.order.copy()
        for positioner in self.auxiliary:
            order.extend([end + order for order in positioner.order])
            end = end + positioner.numberOfLinks

        return order

    @property
    def numberOfLinks(self):
        """ number of links in stack

        :return: number of links
        :rtype: int
        """
        number = self.fixed.numberOfLinks
        for positioner in self.auxiliary:
            number += positioner.numberOfLinks

        return number

    @property
    def bounds(self):
        return [(link.lower_limit, link.upper_limit) for link in self.links]

    def fkine(self, q, ignore_locks=False, setpoint=True):
        """ Moves the stack to specified configuration and returns the forward kinematics
        transformation matrix of the stack.

        :param q: list of joint offsets to move to. The length must be equal to number of links
        :type q: List[float]
        :param ignore_locks: indicates that joint locks should be ignored
        :type ignore_locks: bool
        :param setpoint: indicates that given configuration, q is a setpoint
        :type setpoint: bool
        :return: Forward kinematic transformation matrix
        :rtype: Matrix44
        """
        start, end = 0, self.fixed.numberOfLinks
        T = self.fixed.fkine(q[start:end], ignore_locks=ignore_locks, setpoint=setpoint)
        for link, positioner in zip(self.link_matrix, self.auxiliary):
            start, end = end, end + positioner.numberOfLinks
            T @= link @ positioner.fkine(q[start:end], ignore_locks=ignore_locks, setpoint=setpoint)

        return T

    def ikine(self, current_pose, target_pose,  bounded=True, tol=(1e-2, 1.0), local_max_eval=1000,
              global_max_eval=100):
        return self.ik_solver.solve(current_pose, target_pose, tol=tol, bounded=bounded, local_max_eval=local_max_eval,
                                    global_max_eval=global_max_eval)

    def model(self):
        """ generates 3d model of the stack.

        :return: 3D model of manipulator
        :rtype: Node
        """
        node = self.fixed.model()
        matrix = self.fixed.pose
        for link, positioner in zip(self.link_matrix, self.auxiliary):
            matrix @= link
            node.addChild(positioner.model(matrix))
            matrix @= positioner.pose

        return node

    @property
    def set_points(self):
        """ expected configuration (set-point for all links) of the manipulator

        :return: expected configuration
        :rtype: list[float]
        """
        set_points = []
        set_points.extend(self.fixed.set_points)
        for positioner in self.auxiliary:
            set_points.extend(positioner.set_points)

        return set_points

    @set_points.setter
    def set_points(self, q):
        """ setter for set_points

        :param q: expected configuration
        :type q: list[float]
        """
        for offset, link in zip(q, self.links):
            link.set_point = offset


class Script:
    @unique
    class Key(Enum):
        script = 'script'
        position = 'position'
        count = 'count'
        header = 'header'
        mu_amps = 'mu_amps'
        filename = 'filename'

    def __init__(self, template):
        self.renderer = pystache.Renderer()
        try:
            self.template = template
            self.parsed = pystache.parse(template)
        except UnicodeDecodeError as e:
            raise ValueError('Could not decode the template') from e
        except pystache.parser.ParsingError as e:
            raise ValueError('Template Parsing Failed') from e

        script_tag = ''
        self.header_order = []
        self.keys = {}
        for parse in self.parsed._parse_tree:
            if not (isinstance(parse, pystache.parser._SectionNode) or
                    isinstance(parse, pystache.parser._EscapeNode)):
                continue

            key = Script.Key(parse.key)  # throws ValueError if parse.key is not found
            self.keys[key.value] = ''

            if parse.key == Script.Key.script.value:
                script_tag = parse

        if not script_tag:
            raise ValueError('No script tag!')

        for node in script_tag.parsed._parse_tree:
            if isinstance(node, pystache.parser._EscapeNode):
                key = Script.Key(node.key)  # throws ValueError if parse.key is not found
                self.header_order.append(key.value)
                self.keys[key.value] = ''

        if Script.Key.position.value not in self.keys:
            raise ValueError('No position tag inside the script tag!')

    def render(self):
        return self.renderer.render(self.parsed, self.keys)
