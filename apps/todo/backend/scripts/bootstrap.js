const { initializeTodoDatastore } = require('../src/bootstrap');

const datastore = initializeTodoDatastore();

try {
  process.stdout.write(
    JSON.stringify({
      databasePath: datastore.databasePath,
      status: 'ready',
    })
  );
} finally {
  datastore.close();
}
