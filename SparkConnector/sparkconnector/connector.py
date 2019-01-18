
ipykernel_imported = True
try:
    from ipykernel import zmqshell
except ImportError:
    ipykernel_imported = False

import os, sys, json, logging, tempfile, time, subprocess, socket
from pyspark import SparkConf, SparkContext
from pyspark.sql import SparkSession
from threading import Thread
from string import Formatter
from io import open

class SparkConnector:
    """ Main singleton object for the kernel extension """

    def __init__(self, ipython, log):
        """ Constructor """
        self.ipython = ipython
        self.log = log
        self.connected = False

        self.file_thread = LogReader(self, log)
        log_path = self.file_thread.create_file()
        self.log4j_file = self.create_properties_file(log_path)
        self.file_thread.start()

        self.cluster_name = os.environ.get('SPARK_CLUSTER_NAME')
        self.needs_auth = (self.cluster_name == "nxcals")

    def send(self, msg):
        """Send a message to the frontend"""
        self.comm.send(msg)

    def send_ok(self, page):
        """Send a message to frontend to switch to a specific page """
        self.send({'msgtype': page})

    def send_error(self, page, error):
        """Send a message to frontend to switch to a specific page and append error message"""
        self.send({'msgtype': page, 'error': error})


    def handle_comm_message(self, msg):
        """ Handle message received from frontend """

        action = msg['content']['data']['action']

        # Try to get a kerberos ticket
        if action == 'sparkconn-action-auth':

            # Execute kinit and pipe password directly to the process without exposing it.
            auth_kinit = msg['content']['data']['password']
            p = subprocess.Popen(['kinit'], stdin=subprocess.PIPE, universal_newlines=True)
            p.communicate(input=auth_kinit)

            if p.wait() == 0:
                self.send_ok('sparkconn-config')
            else:
                self.send_error('sparkconn-auth', 'Error obtaining the ticket. Is the password correct?')

        elif action == 'sparkconn-action-connect':

            # The user is already connected, tell the frontend
            if self.connected:
                self.send_ok('sparkconn-connected')
                return

            # As of today, NXCals still requires a valid kerberos token to
            # access their own API.
            if self.needs_auth and not subprocess.call(['klist', '-s']) == 0:
                self.send_error('sparkconn-auth', 'No valid credentials provided.')
                return

            # Check if there's another Spark connection open (check for the port open)
            if self.is_port_in_use():
                self.send_error('sparkconn-config', 'You already opened a Spark connection in this session. Please close it first if you want to open a new one.')
                return

            try:
                # Check if there's already a conf variable
                # If using SparkMonitor, this is defined but is of type SparkConf
                conf = self.ipython.user_ns.get('swan_spark_conf')

                if conf:
                    self.log.warn("conf already exists: %s", conf.toDebugString())
                    if not isinstance(conf, SparkConf):
                        self.send_error('sparkconn-config', 'There is already a "swan_spark_conf" variable defined and is not of type SparkConf.')
                        return
                else:
                    conf = SparkConf()  # Create a new conf

                self.configure(conf, msg['content']['data'])
                sc = SparkContext(conf = conf)
                spark = SparkSession(sc)

                self.ipython.push({"swan_spark_conf": conf, "sc": sc, "spark": spark})  # Add to users namespace
                self.send_ok('sparkconn-connected') # Tell frontend
                self.connected = True

            except Exception as ex:
                self.send_error('sparkconn-config', str(ex))
                self.log.error("Error creating Spark conf", exc_info=True)

        else:
            # Unknown action requested
            self.log.error("Received wrong message: %s", str(msg))
            return

    def register_comm(self):
        """ Register a comm_target which will be used by frontend to start communication """
        self.ipython.kernel.comm_manager.register_target(
            "SparkConnector", self.target_func)

    def target_func(self, comm, msg):
        """ Callback function to be called when a frontend comm is opened """
        self.log.info("Established connection to frontend: %s", str(msg))
        self.comm = comm

        @self.comm.on_msg
        def _recv(msg):
            self.handle_comm_message(msg)

        # Check the current status of the kernel and tell frontend
        # If the user refreshes the page, he will still see the correct state
        if self.connected:
            page = 'sparkconn-connected'
        elif self.needs_auth and not subprocess.call(['klist', '-s']) == 0:
            page = 'sparkconn-auth'
        else:
            page = 'sparkconn-config'

        # Send information about the configs selected on spawner
        self.send({'msgtype': 'sparkconn-action-open',
                   'maxmemory': os.environ.get('MAX_MEMORY'),
                   'cluster': self.cluster_name,
                   'page': page})

    def configure(self, conf, opts):
        """ Configures the provided conf object """

        conf.set('spark.driver.host', os.environ.get('SERVER_HOSTNAME'))
        conf.set('spark.driver.port', os.environ.get('SPARK_PORT_1'))
        conf.set('spark.blockManager.port', os.environ.get('SPARK_PORT_2'))
        conf.set('spark.ui.port', os.environ.get('SPARK_PORT_3'))
        conf.set('spark.master', 'yarn')
        conf.set('spark.authenticate', True)
        conf.set('spark.network.crypto.enabled', True)
        conf.set('spark.authenticate.enableSaslEncryption', True)

        extra_java_options = "-Dlog4j.configuration=file:%s" % self.log4j_file

        analytics_extra_class = "/eos/project/s/swan/public/hadoop-mapreduce-client-core-2.6.0-cdh5.7.6.jar"
        extra_class_path = conf.get('spark.driver.extraClassPath')
        if extra_class_path:
            extra_class_path = extra_class_path + ":" + analytics_extra_class
        else:
            extra_class_path = analytics_extra_class

        if 'options' in opts:
            for name, value in opts['options'].items():
                replaceable_values = {}
                for _, variable, _, _ in Formatter().parse(value):
                    if variable is not None:
                        replaceable_values[variable] = os.environ.get(variable)

                value = value.format(**replaceable_values)

                if name == "spark.driver.extraJavaOptions":
                    extra_java_options = value + " " + extra_java_options
                elif name == "spark.driver.extraClassPath":
                    extra_class_path = extra_class_path + ":" + value
                else:
                    conf.set(name, value)

        ld_library_path = conf.get('spark.executorEnv.LD_LIBRARY_PATH')
        if ld_library_path:
            ld_library_path = ld_library_path + ":" + os.environ.get('LD_LIBRARY_PATH')
        else:
            ld_library_path = os.environ.get('LD_LIBRARY_PATH')


        conf.set('spark.driver.extraJavaOptions', extra_java_options)
        conf.set('spark.driver.extraClassPath', extra_class_path)
        conf.set('spark.executorEnv.LD_LIBRARY_PATH', ld_library_path)

        # Allow the monitoring and filtering of SWAN jobs in the Spark clusters
        app_name = conf.get('spark.app.name')
        conf.set('spark.app.name', app_name + '_swan' if app_name else 'pyspark_shell_swan')

    def create_properties_file(self, log_path):
        """ Creates a configuration file for Spark log4j """

        fd, path = tempfile.mkstemp()
        os.close(fd) # Reopen tempfile because mkstemp opens it in binary format
        f = open(path, 'w')

        __location__ = os.path.realpath(
            os.path.join(os.getcwd(), os.path.dirname(__file__)))
        f_configs = open(os.path.join(__location__, 'log4j_conf'), "r");

        for line in f_configs:
            f.write(line)

        f.write(u'log4j.appender.file.File=%s\n' % log_path)

        f_configs.close()
        f.close()
        self.log.info("Created temporary Log4j configuration file: %s", path)

        return path

    def is_port_in_use(self):
        """ Check if there's already a Spark connection """

        in_use = True
        s = socket.socket()
        try:
            s.connect((socket.gethostname(), int(os.environ.get('SPARK_PORT_1'))))
        except socket.error:
            in_use = False

        s.close()

        return in_use

class LogReader(Thread):
    """ Thread to read a file where the logs from Spark are being written """

    def __init__(self, connector, log):
        self.connector = connector
        self.log = log
        self.path = None
        Thread.__init__(self)

    def create_file(self):
        """ Create a temporary file and return the path to it"""
        fd, path = tempfile.mkstemp()
        os.close(fd)
        self.log.info("Created temporary Log4j log file: %s", path)
        self.path = path
        return path

    def run(self):
        """ Read the log file and send the logs to frontend """
        logfile = open(self.path,"r")
        log_lines = self.follow(logfile)
        for line in log_lines:
            self.connector.send({
                "msgtype": "sparkconn-action-log",
                "msg": line.strip()
            })

    # from "Generator Tricks for Systems Programmers"
    # (http://www.dabeaz.com/generators/)
    # Terminate when the user is connected
    def follow(self, logfile):
        logfile.seek(0,2)
        while not self.connector.connected:
            line = logfile.readline()
            if not line:
                time.sleep(0.1)
                continue
            yield line


def load_ipython_extension(ipython):
    """ Load Jupyter kernel extension """

    log = logging.getLogger('tornado.sparkconnector')
    log.name = 'SparkConnector'
    log.setLevel(logging.INFO)
    log.propagate = True

    if ipykernel_imported:
        if not isinstance(ipython, zmqshell.ZMQInteractiveShell):
            log.error("SparkConnector: Ipython not running through notebook. So exiting.")
            return
    else:
        return

    log.info("Starting SparkConnector Kernel Extension")
    monitor = SparkConnector(ipython, log)
    monitor.register_comm()